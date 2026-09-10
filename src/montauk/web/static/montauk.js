// Progressive enhancement only — the dashboard works without JS.
(function () {
  "use strict";

  // Password show/hide: wrap every <input type=password> that opts in with
  // [data-reveal] in a .pw-field and add an eye toggle button.
  document.querySelectorAll('input[type="password"][data-reveal]').forEach(function (input) {
    var wrap = document.createElement("span");
    wrap.className = "pw-field";
    input.parentNode.insertBefore(wrap, input);
    wrap.appendChild(input);

    var btn = document.createElement("button");
    btn.type = "button";
    btn.className = "pw-toggle";
    btn.setAttribute("aria-label", "Show password");
    btn.setAttribute("aria-pressed", "false");
    btn.textContent = "\u{1F441}"; // eye
    wrap.appendChild(btn);

    btn.addEventListener("click", function () {
      var showing = input.type === "text";
      input.type = showing ? "password" : "text";
      btn.setAttribute("aria-pressed", String(!showing));
      btn.setAttribute("aria-label", showing ? "Show password" : "Hide password");
      btn.textContent = showing ? "\u{1F441}" : "\u{1F648}"; // eye / see-no-evil
      input.focus();
    });
  });

  // Confirmation on destructive forms (CSP-safe alternative to inline onsubmit).
  document.querySelectorAll("form[data-confirm]").forEach(function (form) {
    form.addEventListener("submit", function (e) {
      if (!window.confirm(form.getAttribute("data-confirm"))) e.preventDefault();
    });
  });

  // Auto-submit a filter <select> on change (CSP-safe; degrades to the Search button).
  document.querySelectorAll("select[data-autosubmit]").forEach(function (sel) {
    sel.addEventListener("change", function () {
      if (sel.form) sel.form.submit();
    });
  });

  // Transcripts › extraction progress: poll the status endpoint while a
  // background run is in flight and update the "X / Y" counter; reload the
  // page (to show the new facts) when it finishes. Without JS the page still
  // shows the count at load time and can be refreshed by hand.
  document.querySelectorAll("[data-extract-status]").forEach(function (el) {
    var url = el.getAttribute("data-url");
    var reloaded = false;
    function tick() {
      fetch(url + (url.indexOf("?") < 0 ? "?" : "&") + "t=" + Date.now(), {
        credentials: "same-origin",
        cache: "no-store",
        headers: { Accept: "application/json" },
      })
        .then(function (r) { return r.json(); })
        .then(function (d) {
          el.textContent = "Extracting… " + d.processed + " / " + d.total + " messages processed";
          if (d.active) {
            setTimeout(tick, 2000);
          } else if (!reloaded) {
            reloaded = true;
            location.reload();
          }
        })
        .catch(function () { setTimeout(tick, 4000); });
    }
    tick();
  });

  // Transcripts › participant mapping: show a row's person picker only when
  // that row's role is "a person". Without JS both selects are visible.
  document.querySelectorAll("select[data-participant-role]").forEach(function (roleSel) {
    var row = roleSel.closest("tr") || roleSel.form;
    var personSel = row && row.querySelector("select[data-participant-person]");
    if (!personSel) return;
    var cell = personSel.closest("td") || personSel.parentNode;
    function sync() {
      cell.style.visibility = roleSel.value === "person" ? "" : "hidden";
    }
    roleSel.addEventListener("change", sync);
    sync();
  });

  // Settings › Model & provider: the provider choice drives everything, so
  // show only the fields that provider needs and match the model suggestions
  // to it. Without JS every field is visible (the form still works).
  document.querySelectorAll("select[data-model-provider]").forEach(function (sel) {
    var form = sel.form;
    if (!form) return;
    var modelInput = form.querySelector("input[data-model-input]");

    function apply() {
      var provider = sel.value;
      form.querySelectorAll("[data-when-provider]").forEach(function (el) {
        var allowed = el.getAttribute("data-when-provider").split(/\s+/);
        el.hidden = allowed.indexOf(provider) === -1;
      });
      if (modelInput) {
        var id = modelInput.getAttribute("data-datalist-prefix") + "-" + provider;
        modelInput.setAttribute("list", document.getElementById(id) ? id : "");
        if (!modelInput.value) {
          modelInput.placeholder =
            provider === "none" ? "pick a provider first" : "e.g. " + (function () {
              var dl = document.getElementById(id);
              var opt = dl && dl.querySelector("option");
              return opt ? opt.value : "model name";
            })();
        }
      }
    }
    sel.addEventListener("change", apply);
    apply();
  });

  // Settings › Test connection: run it in the background, show a spinner then
  // a ✓ / ✗ with the message just to the right of the button. Falls back to a
  // normal form submit (full page reload) when this handler is absent.
  document.querySelectorAll("button[data-test-connection]").forEach(function (btn) {
    var form = btn.form;
    if (!form) return;
    var url = btn.getAttribute("formaction") || form.getAttribute("action");

    var status = document.createElement("span");
    status.className = "test-status";
    status.setAttribute("aria-live", "polite");
    btn.parentNode.insertBefore(status, btn.nextSibling);

    btn.addEventListener("click", function (e) {
      e.preventDefault();
      btn.disabled = true;
      status.className = "test-status is-testing";
      status.innerHTML = '<span class="spinner" aria-hidden="true"></span>Testing…';

      fetch(url, {
        method: "POST",
        body: new FormData(form),
        headers: { Accept: "application/json" },
        credentials: "same-origin",
      })
        .then(function (r) {
          return r.json().then(
            function (d) { return d; },
            function () { return { ok: false, message: "unexpected response" }; }
          );
        })
        .catch(function () { return { ok: false, message: "request failed" }; })
        .then(function (res) {
          btn.disabled = false;
          status.className = "test-status " + (res.ok ? "is-ok" : "is-fail");
          status.textContent = (res.ok ? "✓ " : "✗ ") + (res.message || "");
        });
    });
  });
})();
