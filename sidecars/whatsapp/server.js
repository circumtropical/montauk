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
  BufferJSON,
  proto,
  DisconnectReason,
} from "@whiskeysockets/baileys";

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
  contacts: new Map(), // jid -> name
  history: new Map(), // jid -> [msg,...] asc by ts, deduped by id
  live: [], // [{ seq, msg }]
  liveSeq: 0,
  gap: false,
  reconnecting: false,
};

function bumpEpoch() {
  S.epoch += 1;
}

function jidToPhone(jid) {
  const m = /^(\d+)(?:[:.]\d+)?@/.exec(jid || "");
  return m ? "+" + m[1] : null;
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

function senderName(jid, pushName) {
  return pushName || S.contacts.get(jid) || "";
}

function normalize(m) {
  const key = m.key || {};
  const jid = key.remoteJid || "";
  const fromMe = !!key.fromMe;
  const senderJid = fromMe ? S.self?.jid || jid : key.participant || jid;
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

function rememberChat(jid, patch = {}) {
  if (!jid || jid === "status@broadcast") return;
  const cur = S.chats.get(jid) || {
    jid,
    name: S.contacts.get(jid) || jid,
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
  const sock = makeWASocket({
    auth: S.auth,
    logger: log.child({ mod: "baileys" }),
    printQRInTerminal: false,
    markOnlineOnConnect: false, // do not broadcast presence
    syncFullHistory: true,
    shouldSyncHistoryMessage: () => true,
    emitOwnEvents: true, // we archive both sides
    getMessage: async () => undefined, // never re-send; we don't retry decryption
  });
  S.sock = sock;

  sock.ev.on("creds.update", () => bumpEpoch());

  sock.ev.on("connection.update", async (u) => {
    const { connection, lastDisconnect, qr } = u;
    if (qr) {
      S.state = "pairing";
      S.qr = await QRCode.toDataURL(qr, { margin: 1, width: 264 });
      S.qrExpiresAt = Date.now() + 60_000;
    }
    if (connection === "open") {
      S.state = "connected";
      S.qr = null;
      S.lastError = null;
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
        S.state = "degraded";
        if (!S.reconnecting) {
          S.reconnecting = true;
          setTimeout(() => {
            S.reconnecting = false;
            startSocket();
          }, 3000);
        }
      }
    }
  });

  sock.ev.on("contacts.upsert", (cs) => cs.forEach((c) => c.id && S.contacts.set(c.id, c.name || c.notify || S.contacts.get(c.id) || "")));
  sock.ev.on("contacts.update", (cs) => cs.forEach((c) => c.id && c.name && S.contacts.set(c.id, c.name)));
  sock.ev.on("chats.upsert", (cs) => cs.forEach((c) => rememberChat(c.id, { name: c.name || undefined })));
  sock.ev.on("chats.update", (cs) => cs.forEach((c) => c.id && rememberChat(c.id, c.name ? { name: c.name } : {})));

  sock.ev.on("messaging-history.set", ({ chats, contacts, messages }) => {
    (contacts || []).forEach((c) => c.id && S.contacts.set(c.id, c.name || c.notify || S.contacts.get(c.id) || ""));
    (chats || []).forEach((c) => rememberChat(c.id, { name: c.name || undefined }));
    (messages || []).forEach((m) => storeHistory(normalize(m)));
    log.info({ chats: chats?.length, messages: messages?.length }, "history batch");
  });

  sock.ev.on("messages.upsert", ({ messages, type }) => {
    for (const m of messages || []) {
      if (!m.message && !m.key?.fromMe) continue;
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

app.post("/connect", (req, res) => {
  const blob = req.body?.session ?? null;
  try {
    if (S.sock) {
      try { S.sock.ev.removeAllListeners(); } catch {}
      try { S.sock.end(undefined); } catch {}
      S.sock = null;
    }
    loadAuth(blob);
    if (!blob) {
      S.state = "pairing";
      S.chats.clear();
      S.history.clear();
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
      const parts = [];
      if (!isGroup) {
        parts.push({ jid: c.jid, name: c.name || S.contacts.get(c.jid) || "", is_me: false });
        if (S.self) parts.push({ jid: S.self.jid, name: S.self.name, is_me: true });
      }
      return {
        jid: c.jid,
        name: c.name || S.contacts.get(c.jid) || c.jid,
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
