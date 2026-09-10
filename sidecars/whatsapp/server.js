// Montauk WhatsApp sidecar — RECEIVE ONLY.
//
// This process links a WhatsApp device via Baileys and exposes a small
// loopback HTTP API that Montauk polls. It NEVER sends, replies, reacts,
// marks-read, deletes, or edits a message. The only Baileys call that
// writes anything to WhatsApp's servers is an explicit device logout
// (POST /logout), which is the connector "disconnect" operation.
//
// A companion test (tests/phase2/test_connector_readonly.py) fails the
// build if this file references sendMessage / sendReceipt / readMessages /
// chatModify / sendPresenceUpdate / groupParticipantsUpdate / etc.

import express from "express";
import pino from "pino";
import QRCode from "qrcode";
import {
  makeWASocket,
  initAuthCreds,
  makeCacheableSignalKeyStore,
  BufferJSON,
  proto,
  DisconnectReason,
} from "baileys";

const PORT = parseInt(process.env.MONTAUK_WA_SIDECAR_PORT || "8766", 10);
const HOST = process.env.MONTAUK_WA_SIDECAR_HOST || "127.0.0.1";
const TOKEN = process.env.MONTAUK_WA_SIDECAR_TOKEN || "";
const LIVE_CAP = 5000;
const PER_THREAD_CAP = 8000;

const log = pino({ level: process.env.MONTAUK_WA_SIDECAR_LOG || "info" });

// ---- in-memory state -----------------------------------------------------

const S = {
  sock: null,
  auth: null, // { creds, keys } mutated in place by Baileys
  keyStore: {}, // type -> id -> value
  epoch: 0,
  state: "unconfigured", // unconfigured|pairing|connected|degraded|disconnected
  self: null, // { jid, name, phone }
  qr: null, // data URL
  qrExpiresAt: 0,
  lastError: null,
  chats: new Map(), // jid -> { jid, name, is_group, last_ts }
  contacts: new Map(), // jid -> { name, notify }  (address-book name / pushName)
  lidToPn: new Map(), // "<n>@lid" -> "<phone>@s.whatsapp.net"
  history: new Map(), // jid -> [msg,...] asc by ts, deduped by id
  live: [], // [{ seq, msg }]
  liveSeq: 0,
  gap: false,
  reconnecting: false,
  dbg: { contactsUpsert: 0, contactsUpdate: 0, histContacts: 0, msgPush: 0, chatsUpsert: 0 },
};

function bumpEpoch() {
  S.epoch += 1;
}

function isLid(jid) {
  return typeof jid === "string" && jid.endsWith("@lid");
}

// Resolve a WhatsApp "linked id" to the real phone JID when we've seen the
// mapping; otherwise return the jid unchanged.
function resolveJid(jid) {
  if (!jid) return jid;
  if (isLid(jid)) return S.lidToPn.get(jid) || jid;
  return jid;
}

function jidToPhone(jid) {
  const resolved = resolveJid(jid);
  if (!resolved || isLid(resolved)) return null; // a LID is not a phone number
  const m = /^(\d+)(?:[:.]\d+)?@/.exec(resolved);
  return m && resolved.includes("@s.whatsapp.net") ? "+" + m[1] : null;
}

function rememberLidMappings(pairs) {
  for (const p of pairs || []) {
    const lid = p?.lid || p?.lidJid;
    const pn = p?.pn || p?.jid || p?.phoneNumber;
    if (lid && pn) S.lidToPn.set(lid, pn);
  }
}

// ---- auth state (single-blob equivalent of useMultiFileAuthState) -------

function loadAuth(blob) {
  let loaded = null;
  if (blob) {
    try {
      loaded = JSON.parse(blob, BufferJSON.reviver);
    } catch (e) {
      log.warn({ err: String(e) }, "could not parse stored session; starting fresh");
    }
  }
  S.keyStore = loaded?.keys || {};
  const creds = loaded?.creds || initAuthCreds();
  S.auth = {
    creds,
    keys: {
      get: (type, ids) => {
        const out = {};
        for (const id of ids) {
          let val = S.keyStore[type]?.[id];
          if (val && type === "app-state-sync-key") {
            val = proto.Message.AppStateSyncKeyData.fromObject(val);
          }
          out[id] = val;
        }
        return out;
      },
      set: (data) => {
        for (const type of Object.keys(data)) {
          S.keyStore[type] = S.keyStore[type] || {};
          for (const id of Object.keys(data[type])) {
            const v = data[type][id];
            if (v === null || v === undefined) delete S.keyStore[type][id];
            else S.keyStore[type][id] = v;
          }
        }
        bumpEpoch();
      },
    },
  };
}

function serializeAuth() {
  if (!S.auth) return null;
  return JSON.stringify({ creds: S.auth.creds, keys: S.keyStore }, BufferJSON.replacer);
}

// ---- message normalisation --------------------------------------------

function learnContact(rawJid, { name, notify } = {}) {
  const jid = resolveJid(rawJid);
  if (!jid || jid === "status@broadcast" || jid.endsWith("@g.us")) return;
  const cur = S.contacts.get(jid) || {};
  const next = {
    name: name || cur.name || "",
    notify: notify || cur.notify || "",
  };
  S.contacts.set(jid, next);
  // a better name for a direct chat should surface in the thread list
  const best = next.name || next.notify;
  if (best) {
    const chat = S.chats.get(jid);
    if (!chat || !chat.name || chat.name === jid || /^\+?\d[\d ]*$/.test(chat.name)) {
      rememberChat(jid, { name: best });
    }
  }
}

function contactName(jid) {
  const c = S.contacts.get(resolveJid(jid)) || {};
  return c.name || c.notify || "";
}

function senderName(jid, pushName) {
  return pushName || contactName(jid) || "";
}

function normalize(m) {
  const key = m.key || {};
  const jid = resolveJid(key.remoteJid || "");
  const fromMe = !!key.fromMe;
  const senderJid = fromMe
    ? S.self?.jid || jid
    : resolveJid(key.participant || key.participantPn || key.remoteJid || "");
  const ts = Number(m.messageTimestamp?.low ?? m.messageTimestamp ?? 0) || 0;
  const c = m.message || {};
  const unwrapped = c.ephemeralMessage?.message || c.viewOnceMessageV2?.message || c;
  let text =
    unwrapped.conversation ||
    unwrapped.extendedTextMessage?.text ||
    unwrapped.imageMessage?.caption ||
    unwrapped.videoMessage?.caption ||
    unwrapped.documentMessage?.caption ||
    "";
  let kind = "text";
  let attachment_type = null;
  if (unwrapped.imageMessage) { kind = "media"; attachment_type = "image"; }
  else if (unwrapped.videoMessage) { kind = "media"; attachment_type = "video"; }
  else if (unwrapped.audioMessage) { kind = "media"; attachment_type = "audio"; }
  else if (unwrapped.stickerMessage) { kind = "media"; attachment_type = "sticker"; }
  else if (unwrapped.documentMessage) { kind = "media"; attachment_type = "document"; }
  else if (unwrapped.protocolMessage) {
    kind = unwrapped.protocolMessage.type === proto.Message.ProtocolMessage.Type.REVOKE ? "revoked" : "system";
  } else if (!text) {
    kind = "system";
  }
  return {
    provider_id: key.id,
    jid,
    sender_jid: senderJid,
    sender_name: senderName(senderJid, m.pushName),
    from_me: fromMe,
    timestamp: ts,
    text,
    kind,
    attachment_type,
  };
}

function rememberChat(rawJid, patch = {}) {
  const jid = resolveJid(rawJid);
  if (!jid || jid === "status@broadcast" || isLid(jid)) return;
  const cur = S.chats.get(jid) || {
    jid,
    name: contactName(jid) || "",
    is_group: jid.endsWith("@g.us"),
    last_ts: 0,
  };
  S.chats.set(jid, { ...cur, ...patch });
}

function storeHistory(msg) {
  if (!msg.jid || !msg.provider_id) return;
  const arr = S.history.get(msg.jid) || [];
  if (arr.some((x) => x.provider_id === msg.provider_id)) return;
  arr.push(msg);
  arr.sort((a, b) => a.timestamp - b.timestamp);
  if (arr.length > PER_THREAD_CAP) arr.splice(0, arr.length - PER_THREAD_CAP);
  S.history.set(msg.jid, arr);
  if (msg.timestamp) rememberChat(msg.jid, { last_ts: Math.max(msg.timestamp, S.chats.get(msg.jid)?.last_ts || 0) });
}

function pushLive(msg) {
  S.liveSeq += 1;
  S.live.push({ seq: S.liveSeq, msg });
  if (S.live.length > LIVE_CAP) {
    S.live.splice(0, S.live.length - LIVE_CAP);
    S.gap = true;
  }
}

// ---- socket lifecycle ------------------------------------------------

function startSocket() {
  if (!S.auth) loadAuth(null);
  const blog = log.child({ mod: "baileys" });
  const sock = makeWASocket({
    auth: {
      creds: S.auth.creds,
      keys: makeCacheableSignalKeyStore(S.auth.keys, blog),
    },
    logger: blog,
    printQRInTerminal: false,
    markOnlineOnConnect: false, // do not broadcast presence
    syncFullHistory: true,
    shouldSyncHistoryMessage: () => true,
    emitOwnEvents: true, // we archive both sides
    getMessage: async () => undefined, // never re-send; we don't retry decryption
  });
  S.sock = sock;

  sock.ev.on("creds.update", () => bumpEpoch());
  sock.ev.on("lid-mapping.update", (m) => rememberLidMappings([m]));

  sock.ev.on("connection.update", async (u) => {
    const { connection, lastDisconnect, qr } = u;
    if (qr) {
      S.state = "pairing";
      S.qr = await QRCode.toDataURL(qr, { margin: 1, width: 264 });
      S.qrExpiresAt = Date.now() + 60_000;
    }
    if (connection === "connecting" && S.state !== "connected") {
      // a transient state -- do NOT report "disconnected" or the driver
      // will hammer /connect and churn the socket mid-handshake.
      S.state = S.state === "pairing" ? "pairing" : "connecting";
    }
    if (connection === "open") {
      S.state = "connected";
      S.qr = null;
      S.lastError = null;
      S.reconnectDelay = 3000;
      const u2 = sock.user || {};
      S.self = { jid: u2.id, name: u2.name || u2.verifiedName || "", phone: jidToPhone(u2.id) };
      log.info({ self: S.self?.phone }, "whatsapp connected");
    }
    if (connection === "close") {
      const code = lastDisconnect?.error?.output?.statusCode;
      S.lastError = lastDisconnect?.error?.message || null;
      if (code === DisconnectReason.loggedOut) {
        S.state = "disconnected";
        S.auth = null;
        S.keyStore = {};
        S.self = null;
        log.warn("whatsapp logged out on the phone side");
      } else {
        // Keep reconnecting ourselves with backoff. Stay "connecting" (not
        // "disconnected") so the driver leaves us alone while we retry.
        S.state = S.self ? "connecting" : "degraded";
        if (!S.reconnecting) {
          S.reconnecting = true;
          const delay = Math.min(S.reconnectDelay || 3000, 60_000);
          S.reconnectDelay = delay * 2;
          setTimeout(() => {
            S.reconnecting = false;
            startSocket();
          }, delay);
        }
      }
    }
  });

  const learnFromContacts = (cs, tag) =>
    (cs || []).forEach((c) => {
      if (!c.id) return;
      if (tag) S.dbg[tag] = (S.dbg[tag] || 0) + 1;
      learnContact(c.id, {
        name: c.name || c.verifiedName || c.notify,
        notify: c.notify,
      });
    });
  sock.ev.on("contacts.upsert", (cs) => learnFromContacts(cs, "contactsUpsert"));
  sock.ev.on("contacts.update", (cs) => learnFromContacts(cs, "contactsUpdate"));
  sock.ev.on("chats.upsert", (cs) =>
    cs.forEach((c) => {
      S.dbg.chatsUpsert++;
      if (c.name) learnContact(c.id, { name: c.name });
      rememberChat(c.id, c.name ? { name: c.name } : {});
    }),
  );
  sock.ev.on("chats.update", (cs) =>
    cs.forEach((c) => c.id && c.name && (learnContact(c.id, { name: c.name }), rememberChat(c.id, { name: c.name }))),
  );

  const learnFromMessage = (m) => {
    if (m.pushName && !m.key?.fromMe) {
      S.dbg.msgPush++;
      const who = resolveJid(m.key?.participant || m.key?.participantPn || m.key?.remoteJid || "");
      learnContact(who, { notify: m.pushName });
    }
  };

  sock.ev.on("messaging-history.set", ({ chats, contacts, messages, lidPnMappings }) => {
    rememberLidMappings(lidPnMappings);
    learnFromContacts(contacts, "histContacts");
    (chats || []).forEach((c) => {
      if (c.name) learnContact(c.id, { name: c.name });
      rememberChat(c.id, c.name ? { name: c.name } : {});
    });
    (messages || []).forEach((m) => {
      learnFromMessage(m);
      storeHistory(normalize(m));
    });
    log.info({ chats: chats?.length, messages: messages?.length }, "history batch");
  });

  sock.ev.on("messages.upsert", ({ messages, type }) => {
    for (const m of messages || []) {
      if (!m.message && !m.key?.fromMe) continue;
      learnFromMessage(m);
      const norm = normalize(m);
      storeHistory(norm);
      if (type === "notify") pushLive(norm);
    }
  });
}

// ---- HTTP API -------------------------------------------------------

const app = express();
app.use(express.json({ limit: "8mb" }));
app.use((req, res, next) => {
  if (!TOKEN) return res.status(500).json({ error: "sidecar token not configured" });
  const got = (req.headers.authorization || "").replace(/^Bearer\s+/i, "");
  if (got !== TOKEN) return res.status(401).json({ error: "unauthorized" });
  next();
});

function statusPayload() {
  return {
    state: S.state,
    self: S.self,
    qr: S.qr,
    qr_expires_at: S.qrExpiresAt ? Math.floor(S.qrExpiresAt / 1000) : null,
    last_error: S.lastError,
    session_epoch: S.epoch,
  };
}

app.get("/status", (_req, res) => res.json(statusPayload()));

// debug: how much identity info we've learned
app.get("/debug", (_req, res) => {
  const chats = [...S.chats.values()];
  const named = chats.filter((c) => c.name && !/^\+?[\d ]+$/.test(c.name) && !c.name.includes("@"));
  res.json({
    chats: chats.length,
    contacts: S.contacts.size,
    contacts_with_name: [...S.contacts.values()].filter((c) => c.name).length,
    contacts_with_notify: [...S.contacts.values()].filter((c) => c.notify).length,
    named_chats: named.length,
    sample_contacts: [...S.contacts.entries()].slice(0, 8).map(([k, v]) => ({ jid: k, ...v })),
    counters: S.dbg,
  });
});

app.post("/connect", (req, res) => {
  const blob = req.body?.session ?? null;
  // Idempotent: if a socket is already up (or mid-handshake) on this same
  // session, leave it alone -- tearing it down mid-init is what causes the
  // "init queries timed out" / decrypt-counter errors.
  const busy = S.sock && ["connected", "connecting", "pairing"].includes(S.state);
  if (busy && (blob ? blob === S.lastLoadedBlob : S.state === "pairing")) {
    return res.json(statusPayload());
  }
  try {
    if (S.sock) {
      try { S.sock.ev.removeAllListeners(); } catch {}
      try { S.sock.end(undefined); } catch {}
      S.sock = null;
    }
    loadAuth(blob);
    S.lastLoadedBlob = blob;
    S.reconnectDelay = 3000;
    if (!blob) {
      S.state = "pairing";
      S.chats.clear();
      S.history.clear();
      S.contacts.clear();
      S.live = [];
      S.liveSeq = 0;
    }
    startSocket();
    res.json(statusPayload());
  } catch (e) {
    S.lastError = String(e);
    log.error({ err: String(e) }, "connect failed");
    res.status(500).json({ error: String(e) });
  }
});

app.get("/session", (_req, res) => res.json({ session: serializeAuth() }));

app.get("/threads", (_req, res) => {
  const threads = [...S.chats.values()]
    .filter((c) => c.jid && c.jid !== "status@broadcast")
    .map((c) => {
      const isGroup = c.jid.endsWith("@g.us");
      const name = c.name || contactName(c.jid) || "";
      const parts = [];
      if (!isGroup) {
        parts.push({ jid: c.jid, name, is_me: false });
        if (S.self) parts.push({ jid: S.self.jid, name: S.self.name, is_me: true });
      }
      return {
        jid: c.jid,
        name: name || jidToPhone(c.jid) || c.jid,
        phone: isGroup ? null : jidToPhone(c.jid),
        is_group: isGroup,
        participants: parts,
        last_ts: c.last_ts || null,
        count_estimate: (S.history.get(c.jid) || []).length || null,
      };
    })
    .sort((a, b) => (b.last_ts || 0) - (a.last_ts || 0));
  res.json({ threads });
});

app.post("/history", (req, res) => {
  const { jid, before_cursor, limit } = req.body || {};
  const all = S.history.get(jid) || [];
  const beforeTs = before_cursor?.before_ts ?? null;
  const eligible = beforeTs == null ? all : all.filter((m) => m.timestamp < beforeTs);
  const lim = Math.min(Math.max(parseInt(limit || "200", 10), 1), 1000);
  const page = eligible.slice(Math.max(0, eligible.length - lim));
  const oldest = page.length ? page[0].timestamp : beforeTs;
  res.json({
    messages: page,
    next_cursor: page.length && page.length >= lim ? { before_ts: oldest } : null,
    done: page.length < lim,
  });
});

app.get("/inbox", (req, res) => {
  const after = parseInt(req.query.after || "0", 10) || 0;
  const items = S.live.filter((x) => x.seq > after);
  const gap = S.gap && (items.length === 0 || items[0].seq > after + 1);
  S.gap = false;
  res.json({
    messages: items.map((x) => ({ ...x.msg, seq: x.seq })),
    seq: S.live.length ? S.live[S.live.length - 1].seq : after,
    gap,
  });
});

app.post("/disconnect", (_req, res) => {
  try { S.sock?.end(undefined); } catch {}
  S.sock = null;
  S.state = "disconnected";
  res.json(statusPayload());
});

app.post("/logout", async (_req, res) => {
  try {
    await S.sock?.logout(); // unlinks THIS device only; the §10 disconnect op
  } catch (e) {
    log.warn({ err: String(e) }, "logout error");
  }
  S.sock = null;
  S.auth = null;
  S.keyStore = {};
  S.self = null;
  S.state = "disconnected";
  res.json(statusPayload());
});

app.get("/health", (_req, res) => res.json({ ok: true, state: S.state }));

app.listen(PORT, HOST, () => log.info(`montauk-whatsapp sidecar on http://${HOST}:${PORT}`));
