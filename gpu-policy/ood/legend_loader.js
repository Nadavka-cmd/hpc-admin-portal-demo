// legend_loader.js
// Drop-in replacement for a hardcoded `const partitionLegend = {...}` object
// in an Open OnDemand interactive-app form.js.
//
// It reads the canonical legend that form.yml.erb embedded into the page as
// base64 (inside an element with id "gpu-policy-legend"). Decoded UTF-8-safe
// via TextDecoder -- atob() alone mangles multi-byte chars (e.g. en-dashes in
// partition labels). Falls back to an empty object so a missing/blocked blob
// degrades gracefully instead of throwing.
const partitionLegend = (function () {
  try {
    var el = document.getElementById("gpu-policy-legend");
    if (el && el.textContent.trim()) {
      var bin = atob(el.textContent.trim());
      var bytes = Uint8Array.from(bin, function (c) { return c.charCodeAt(0); });
      return JSON.parse(new TextDecoder("utf-8").decode(bytes));
    }
  } catch (e) {}
  return {};
})();
