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
})();
