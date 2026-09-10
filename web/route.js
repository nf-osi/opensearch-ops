// Shareable deep links. The whole app state a link needs to carry is three parts, in
// order of scope:
//
//   #/<tab>/<index-table>/<section>
//   #/results                       the results dashboard, default index
//   #/results/tools                 nf-tools selected
//   #/results/tools/failures        …scrolled to "Searches legacy search missed"
//   #/lab                           the search lab
//
// Two owners read it: app.js applies the tab, bench.js applies the index and section.
// Both go through applyRoute so a pasted link, a Back press and a click all land in the
// same place.

const SEP = "/";

export function parseHash(hash = location.hash) {
  const [tab, table, section] = hash.replace(/^#\/?/, "").split(SEP).map(decodeURIComponent);
  return { tab: tab || null, table: table || null, section: section || null };
}

export function routeHash({ tab, table, section }) {
  // a section without an index (or an index without a tab) can't be addressed, so the
  // trailing parts drop rather than producing "#/results//failures"
  const parts = [tab, table, section].map((p) => p || "");
  const kept = parts.slice(0, parts.findIndex((p) => !p) === -1 ? 3 : parts.findIndex((p) => !p));
  return `#/${kept.map(encodeURIComponent).join(SEP)}`;
}

/** Absolute URL for the current route — what the anchor buttons copy. */
export const routeUrl = (patch = {}) =>
  `${location.origin}${location.pathname}${routeHash({ ...parseHash(), ...patch })}`;

/** Update the URL without re-applying state (the caller has already rendered it).
 *  `push` adds a history entry, so Back returns to the previous view. */
export function setRoute(patch, { push = false } = {}) {
  const next = { ...parseHash(), ...patch };
  const hash = routeHash(next);
  if (hash === location.hash) return next;
  try {
    history[push ? "pushState" : "replaceState"](null, "", hash);
  } catch {
    location.hash = hash;                 // file:// or a blocked history API
  }
  return next;
}

/** Re-apply the URL after Back/Forward or a hand-edited hash. Programmatic writes go
 *  through setRoute (which fires neither event), so this only runs on real navigation. */
export function onRoute(apply) {
  let last = location.hash;
  const fire = () => {
    if (location.hash === last) return;
    last = location.hash;
    apply(parseHash());
  };
  window.addEventListener("hashchange", fire);
  window.addEventListener("popstate", fire);
}

/** Scroll a section into view. Waits a frame so it works on first paint, when the
 *  section may have been rendered microseconds ago. */
export function scrollToSection(id, { smooth = true } = {}) {
  if (!id) return;
  requestAnimationFrame(() => {
    const node = document.getElementById(id);
    if (!node) return;
    node.scrollIntoView({ behavior: smooth ? "smooth" : "auto", block: "start" });
  });
}

/** The ¶-style control beside a section heading: copies a link to that section, and
 *  puts it in the URL bar so the browser's own share/bookmark picks it up. */
export function anchorLink(slug, { table = null, tab = "results" } = {}) {
  const b = document.createElement("button");
  b.type = "button";
  b.className = "anchor-link";
  b.title = "Copy link to this section";
  b.setAttribute("aria-label", "Copy link to this section");
  b.innerHTML =
    `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
          stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
       <path d="M10 13a5 5 0 0 0 7.5.5l3-3a5 5 0 0 0-7-7l-1.5 1.5"/>
       <path d="M14 11a5 5 0 0 0-7.5-.5l-3 3a5 5 0 0 0 7 7L12 19"/>
     </svg>`;
  b.addEventListener("click", async () => {
    const url = routeUrl({ tab, table, section: slug });
    setRoute({ tab, table, section: slug }, { push: true });
    try {
      await navigator.clipboard.writeText(url);
      b.classList.add("is-copied");
      b.title = "Link copied";
      setTimeout(() => { b.classList.remove("is-copied"); b.title = "Copy link to this section"; }, 1600);
    } catch {
      // clipboard blocked (no permission / insecure context) — the URL bar still carries
      // the link, which is the part that matters
    }
  });
  return b;
}
