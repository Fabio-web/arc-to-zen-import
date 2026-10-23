const sidebar = args[0];
const win = Services.wm.getMostRecentWindow("navigator:browser");
const browser = win.gBrowser;
const manager = win.gZenWorkspaces;
const {SessionStore} = ChromeUtils.importESModule(
  "moz-src:///browser/components/sessionstore/SessionStore.sys.mjs"
);
await SessionStore.promiseInitialized;
await SessionStore.promiseAllWindowsRestored;
await manager.promiseInitialized;
if (!Services.io.offline) throw new Error("Le réordonnancement exige le mode hors ligne.");
const result = {spaces: 0, folders: 0, pinned_tabs: 0, open_tabs: 0, favorites: 0,
                missing_spaces: 0, missing_folders: 0, missing_tabs: 0};
const canonical = value => {
  try { return Services.io.newURI(value).spec; } catch (_) { return value; }
};
const isFolder = node => node.localName === "zen-folder";
const isTab = node => browser.isTab(node) && !node.hasAttribute("zen-empty-tab");
const children = container => Array.from(container?.children || [])
  .filter(node => isFolder(node) || isTab(node));
const stateCache = new Map();
const tabInfo = tab => {
  if (!stateCache.has(tab)) {
    const state = JSON.parse(SessionStore.getTabState(tab));
    const entry = state.entries?.[(state.index || state.entries.length) - 1];
    stateCache.set(tab, {
      urls: new Set([entry?.url, state._zenPinnedInitialState?.entry?.url,
                     tab._zenPinnedInitialState?.entry?.url].filter(Boolean).map(canonical)),
      title: tab.zenStaticLabel || entry?.title || "",
    });
  }
  return stateCache.get(tab);
};
const matchTab = (node, available) => {
  const candidates = available.filter(tab => isTab(tab) && tabInfo(tab).urls.has(canonical(node.url)));
  return candidates.find(tab => tabInfo(tab).title === node.title) || candidates[0];
};
const reorder = (container, ordered) => {
  if (ordered.length < 2) return;
  const members = new Set(ordered);
  const current = Array.from(container.children).filter(node => members.has(node));
  if (current.every((node, index) => node === ordered[index])) return;


  let anchor = current[current.length - 1].nextSibling;
  for (const node of [...ordered].reverse()) {
    const before = anchor;
    browser.zenHandleTabMove(node, () => container.insertBefore(node, before));
    anchor = node;
  }
  const actual = Array.from(container.children).filter(node => members.has(node));
  if (actual.some((node, index) => node !== ordered[index])) {
    throw new Error("Zen n’a pas conservé l’ordre demandé.");
  }
};
const pinned = (nodes, container) => {
  const available = children(container);
  const ordered = [];
  for (const node of nodes || []) {
    if (Array.isArray(node.children)) {
      const folder = available.find(item => isFolder(item) && item.label === node.title);
      if (!folder) { result.missing_folders++; continue; }
      available.splice(available.indexOf(folder), 1);
      ordered.push(folder);
      result.folders++;
      pinned(node.children, folder.groupContainer);
    } else {
      const tab = matchTab(node, available);
      if (!tab) { result.missing_tabs++; continue; }
      available.splice(available.indexOf(tab), 1);
      ordered.push(tab);
      result.pinned_tabs++;
    }
  }
  reorder(container, ordered);
};
const leaves = function* (nodes) {
  for (const node of nodes || []) {
    if (Array.isArray(node.children)) yield* leaves(node.children);
    else yield node;
  }
};
const flat = (nodes, container, counter) => {
  const available = children(container);
  const ordered = [];
  for (const node of leaves(nodes)) {
    const tab = matchTab(node, available);
    if (!tab) { result.missing_tabs++; continue; }
    available.splice(available.indexOf(tab), 1);
    ordered.push(tab);
    result[counter]++;
  }
  reorder(container, ordered);
};
const spaces = [...manager.getWorkspaces()];
for (const source of sidebar.spaces || []) {
  const index = spaces.findIndex(space => space.name === source.title);
  if (index < 0) { result.missing_spaces++; continue; }
  const [space] = spaces.splice(index, 1);
  const element = manager.workspaceElement(space.uuid);
  if (!element) { result.missing_spaces++; continue; }
  pinned(source.bookmarks, element.pinnedTabsContainer);
  flat(source.tabs, element.tabsContainer, "open_tabs");
  result.spaces++;
}
flat(sidebar.favorites, manager.getEssentialsSection(0), "favorites");
browser.tabContainer._invalidateCachedTabs();
manager.updateTabsContainers();
return result;
