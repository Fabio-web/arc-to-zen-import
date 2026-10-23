const { SessionStore } = ChromeUtils.importESModule(
  "moz-src:///browser/components/sessionstore/SessionStore.sys.mjs"
);
await SessionStore.promiseInitialized;
await SessionStore.promiseAllWindowsRestored;
const browserWindow = Services.wm.getMostRecentWindow("navigator:browser");
await browserWindow.gZenWorkspaces.promiseInitialized;
const browser = browserWindow.gBrowser;
const manager = browserWindow.gZenWorkspaces;
browser.tabContainer._invalidateCachedTabs();

const spaces = manager.getWorkspaces();
const spaceNames = new Map(spaces.map(space => [space.uuid, space.name]));
const storedTabs = manager.allStoredTabs;
const domTabs = Array.from(browserWindow.document.querySelectorAll("tab"))
  .filter(tab => browser.isTab(tab));
const tabSet = new Set(storedTabs);
const folders = Array.from(browserWindow.document.querySelectorAll("zen-folder"));
const folderPath = group => {
  const result = [], seen = new Set();
  while (group && !seen.has(group)) {
    seen.add(group);
    result.unshift(group.label || "");
    group = group.group;
  }
  return result;
};
const tabRows = [], storedRows = [];
const counts = {
  spaces: spaces.length, folders: folders.length, tabs: 0,
  pinned_tabs: 0, open_tabs: 0, favorites: 0, pending_tabs: 0,
  stored_web_tabs: 0, dom_web_tabs: 0, detached_from_store: 0,
  workspace_mismatches: 0, bookmarks: 0, bookmark_folders: 0,
  spaces_with_icons: spaces.filter(space => Boolean(space.icon)).length,
};
for (const tab of domTabs) {
  if (tab.hasAttribute("zen-empty-tab")) continue;
  const state = JSON.parse(SessionStore.getTabState(tab));
  const entry = state.entries?.[(state.index || state.entries.length) - 1];
  if (!entry?.url || !/^(?:https?|ftp|file):/.test(entry.url)) continue;
  const essential = tab.hasAttribute("zen-essential");
  const workspaceId = tab.getAttribute("zen-workspace-id");
  const parentSpaceId = tab.closest("zen-workspace")?.id || "";
  const row = [
    entry.url, tab.zenStaticLabel || entry.title || "",
    Boolean(tab.pinned), essential,
    essential ? "" : spaceNames.get(workspaceId) || workspaceId || "",
    folderPath(tab.group),
    essential ? "" : spaceNames.get(parentSpaceId) || parentSpaceId || "",
  ];
  tabRows.push(row);
  counts.dom_web_tabs++;
  counts.tabs++;
  counts.pending_tabs += Number(tab.hasAttribute("pending"));
  if (essential) counts.favorites++;
  else if (tab.pinned) counts.pinned_tabs++;
  else counts.open_tabs++;
  if (!essential && workspaceId !== parentSpaceId) counts.workspace_mismatches++;
  if (tabSet.has(tab)) {
    storedRows.push(row);
    counts.stored_web_tabs++;
  } else {
    counts.detached_from_store++;
  }
}
const folderRows = folders.map(folder => [
  folderPath(folder),
  spaceNames.get(folder.getAttribute("zen-workspace-id")) || "",
  Boolean(folder.collapsed),
]);
const { PlacesUtils } = ChromeUtils.importESModule("resource://gre/modules/PlacesUtils.sys.mjs");
const bookmarkRootGuid = Services.prefs.getStringPref("arc-to-zen.bookmark-root", "");
if (!bookmarkRootGuid) throw new Error("Racine des marque-pages importés introuvable.");
const bookmarkTree = await PlacesUtils.promiseBookmarksTree(bookmarkRootGuid);
if (!bookmarkTree) throw new Error("Arborescence des marque-pages importés introuvable.");
const bookmarkState = node => {
  if (Array.isArray(node.children)) {
    counts.bookmark_folders++;

    return { title: node.title || "", children: node.children.map(bookmarkState) };
  }
  const url = node.url?.href || node.url || node.uri;
  if (url) {
    counts.bookmarks++;
    return { title: node.title || "", url: String(url) };
  }
  counts.bookmark_folders++;
  return { title: node.title || "", children: [] };
};
const bookmarks = bookmarkState(bookmarkTree);

const orderedTree = container => Array.from(container?.children || []).flatMap(node => {
  if (node.localName === "zen-folder") {
    return [{folder: node.label || "", children: orderedTree(node.groupContainer)}];
  }
  if (!browser.isTab(node) || node.hasAttribute("zen-empty-tab")) return [];
  const state = JSON.parse(SessionStore.getTabState(node));
  const entry = state.entries?.[(state.index || state.entries.length) - 1];
  if (!entry?.url || !/^(?:https?|ftp|file):/.test(entry.url)) return [];
  return [{url: entry.url, title: node.zenStaticLabel || entry.title || ""}];
});
const orderedLayout = {
  essentials: orderedTree(manager.getEssentialsSection(0)),
  spaces: spaces.map(space => {
    const element = manager.workspaceElement(space.uuid);
    return {name: space.name, icon: space.icon || "", pinned: orderedTree(element?.pinnedTabsContainer),
            open: orderedTree(element?.tabsContainer)};
  }),
};
const sorted = rows => rows.sort((a, b) => JSON.stringify(a).localeCompare(JSON.stringify(b)));
const fingerprint = value => {
  const bytes = new TextEncoder().encode(JSON.stringify(value));
  const hash = Cc["@mozilla.org/security/hash;1"].createInstance(Ci.nsICryptoHash);
  hash.init(Ci.nsICryptoHash.SHA256);
  hash.update(bytes, bytes.length);
  return Array.from(hash.finish(false), ch => ch.charCodeAt(0).toString(16).padStart(2, "0")).join("");
};
return {
  counts,
  fingerprints: {
    tabs: fingerprint(sorted(tabRows)),
    stored_tabs: fingerprint(sorted(storedRows)),
    folders: fingerprint(sorted(folderRows)),
    spaces: fingerprint(sorted(spaces.map(space => [space.uuid, space.name, space.icon || ""]))),
    bookmarks: fingerprint(bookmarks),
    ordered_layout: fingerprint(orderedLayout),
  },
};
