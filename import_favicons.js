if (!Services.io.offline) throw new Error("L'import des icônes exige le mode hors ligne.");
const input = args[0];
if (!input || typeof input !== "object" || Array.isArray(input)) {
  throw new Error("Cache d'icônes invalide.");
}
const verifyOnly = args[1] === "verify";
const { PlacesUtils } = ChromeUtils.importESModule("resource://gre/modules/PlacesUtils.sys.mjs");
const { SessionStore } = ChromeUtils.importESModule(
  "moz-src:///browser/components/sessionstore/SessionStore.sys.mjs"
);
const { TabStateCache } = ChromeUtils.importESModule(
  "moz-src:///browser/components/sessionstore/TabStateCache.sys.mjs"
);
await SessionStore.promiseInitialized;
await SessionStore.promiseAllWindowsRestored;
const win = Services.wm.getMostRecentWindow("navigator:browser");
await win.gZenWorkspaces.promiseInitialized;
const browser = win.gBrowser;
const result = {
  pages_imported: 0, pages_cached: 0, rejected: 0,
  tabs_with_cached_icon: 0, tabs_updated: 0, pending_tabs_updated: 0,
  tab_images_matching: 0, tab_images_missing: 0,
};
const icons = new Map();
const rasterData = /^data:image\/(?:png|jpeg|gif|webp|x-icon|vnd\.microsoft\.icon);base64,[A-Za-z0-9+/]+={0,2}$/;
for (const [pageURL, image] of Object.entries(input)) {
  try {
    const pageURI = Services.io.newURI(pageURL);
    if (!["http", "https", "ftp", "file"].includes(pageURI.scheme) ||
        typeof image !== "string" || image.length > 2_000_000 || !rasterData.test(image)) {
      result.rejected++;
      continue;
    }
    icons.set(pageURI.spec, image);
    if (!verifyOnly) {


      const iconURI = Services.io.newURI(pageURI.specIgnoringRef + "#arc-imported-favicon");
      await PlacesUtils.favicons.setFaviconForPage(
        pageURI, iconURI, Services.io.newURI(image),
        (Date.now() + 7 * 86400000) * 1000, false
      );
      result.pages_imported++;
    }
    const cached = await PlacesUtils.favicons.getFaviconForPage(pageURI);
    if (cached?.dataURI?.schemeIs("data")) result.pages_cached++;
  } catch (_) {

    result.rejected++;
  }
}


const tabs = new Set([
  ...win.gZenWorkspaces.allStoredTabs,
  ...Array.from(win.document.querySelectorAll("tab")).filter(tab => browser.isTab(tab)),
]);
for (const tab of tabs) {
  if (tab.hasAttribute("zen-empty-tab")) continue;
  const state = JSON.parse(SessionStore.getTabState(tab));
  const entry = state.entries?.[(state.index || state.entries.length) - 1];
  if (!entry?.url) continue;
  let pageURL;
  try { pageURL = Services.io.newURI(entry.url).spec; } catch (_) { continue; }
  const image = icons.get(pageURL);
  if (!image) continue;
  result.tabs_with_cached_icon++;
  if (!verifyOnly) {
    if (tab._zenPinnedInitialState) tab._zenPinnedInitialState.image = image;
    browser.setIcon(tab, image);
    TabStateCache.update(tab.permanentKey, { image });
    result.tabs_updated++;
    result.pending_tabs_updated += Number(tab.hasAttribute("pending"));
  }
  const saved = JSON.parse(SessionStore.getTabState(tab));
  if (browser.getIcon(tab) === image && saved.image === image) result.tab_images_matching++;
  else result.tab_images_missing++;
}
return result;
