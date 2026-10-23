const sidebar = args[0];
if (!sidebar || !Array.isArray(sidebar.spaces) || !Array.isArray(sidebar.favorites)) {
  throw new Error("Structure de la barre latérale Arc invalide.");
}

const browserWindow = Services.wm.getMostRecentWindow("navigator:browser");
if (!browserWindow?.gBrowser || !browserWindow.gZenWorkspaces) {
  throw new Error("Fenêtre Zen indisponible.");
}
const browser = browserWindow.gBrowser;
const workspaceManager = browserWindow.gZenWorkspaces;
await workspaceManager.promiseInitialized;
if (!workspaceManager.currentWindowIsSyncing || !workspaceManager.workspaceEnabled) {
  throw new Error("Les espaces Zen ne sont pas disponibles dans cette fenêtre.");
}
if (!Services.io.offline) {
  throw new Error("L'import des onglets exige le mode hors ligne.");
}
if (Services.prefs.getBoolPref("arc-to-zen.layout-imported", false)) {
  throw new Error("La barre latérale a déjà été importée dans ce profil.");
}

const { SessionStore } = ChromeUtils.importESModule(
  "moz-src:///browser/components/sessionstore/SessionStore.sys.mjs"
);
const { PlacesUtils } = ChromeUtils.importESModule("resource://gre/modules/PlacesUtils.sys.mjs");
const { E10SUtils } = ChromeUtils.importESModule("resource://gre/modules/E10SUtils.sys.mjs");
await SessionStore.promiseInitialized;

const report = {
  spaces: 0, folders: 0, pinned_tabs: 0, open_tabs: 0,
  favorites: 0, bookmarks: 0, skipped: {},
  notes: ["Les groupes d'onglets ouverts sont aplatis ; leur arborescence reste dans les marque-pages."],
};
const skipped = reason => {
  report.skipped[reason] = (report.skipped[reason] || 0) + 1;
};
const cleanTitle = (value, fallback) =>
  typeof value === "string" && value.trim() ? value : fallback;
const urlFor = node => {
  if (typeof node?.url !== "string") return null;
  try {
    const uri = Services.io.newURI(node.url);


    return ["http", "https", "ftp", "file"].includes(uri.scheme) ? uri.spec : null;
  } catch {
    return null;
  }
};
const walkLeaves = function* (nodes) {
  for (const node of nodes || []) {
    if (Array.isArray(node?.children)) yield* walkLeaves(node.children);
    else yield node;
  }
};


const spaces = [];
for (const sourceSpace of sidebar.spaces) {
  const title = cleanTitle(sourceSpace.title, "Espace Arc");
  const sourceIcon = sourceSpace.icon;


  const icon = typeof sourceIcon === "string" && sourceIcon.length <= 80 &&
    !/[A-Za-z/:<>\s]/.test(sourceIcon) &&
    /[\p{Extended_Pictographic}\p{Regional_Indicator}\u20E3]/u.test(sourceIcon)
      ? sourceIcon : undefined;
  const space = await workspaceManager.createAndSaveWorkspace(title, icon, false, 0);
  if (!space || space.name !== title || (icon && space.icon !== icon) ||
      !workspaceManager.workspaceElement(space.uuid)) {
    throw new Error("Impossible de créer un espace Zen.");
  }
  spaces.push({ source: sourceSpace, target: space });
  report.spaces++;
}
if (!spaces.length && sidebar.favorites.length) {
  const space = await workspaceManager.createAndSaveWorkspace("Arc", undefined, false, 0);
  if (!space) throw new Error("Impossible de créer l'espace Arc.");
  spaces.push({ source: { bookmarks: [], tabs: [] }, target: space });
  report.spaces++;
}

const makeTab = (node, workspaceId, pinned, essential = false) => {
  const url = urlFor(node);
  if (!url) {
    skipped("unsupported_or_invalid_tab_url");
    return null;
  }
  const title = cleanTitle(node.title, url);
  const uri = Services.io.newURI(url);
  const principal = Services.scriptSecurityManager.createContentPrincipal(uri, {});
  const tab = browser.addTab(url, {
    triggeringPrincipal: principal,
    createLazyBrowser: true,
    lazyTabTitle: title,
    skipLoad: true,
    skipRoute: true,
    inBackground: true,
    skipAnimation: true,
    skipBackgroundNotify: true,
    userContextId: 0,
    zenWorkspaceId: workspaceId,
  });
  if (!tab) throw new Error("Zen n'a pas pu créer un onglet.");
  const entry = {
    url, title,
    triggeringPrincipal_base64: E10SUtils.serializePrincipal(principal),
  };
  const state = {
    entries: [entry], index: 1, userContextId: 0,
    zenWorkspace: workspaceId,
    zenStaticLabel: title,
    pinned,
  };
  if (pinned) {

    state._zenPinnedInitialState = { entry, image: "" };
    tab._zenPinnedInitialState = state._zenPinnedInitialState;
  }
  SessionStore.setTabState(tab, state);


  workspaceManager.moveTabToWorkspace(tab, workspaceId);
  if (essential) {
    const added = browserWindow.gZenPinnedTabManager.addToEssentials(tab, { replicating: true });
    if (!added || !tab.hasAttribute("zen-essential")) {
      throw new Error("Zen n'a pas pu créer un favori Essential.");
    }
  }
  return tab;
};

const makePinnedNodes = (nodes, workspaceId, parent = null) => {
  for (const node of nodes || []) {
    if (Array.isArray(node?.children)) {
      const folder = browserWindow.gZenFolders.createFolder([], {
        id: Services.uuid.generateUUID().toString().slice(1, -1),
        label: cleanTitle(node.title, "Dossier Arc"),
        workspaceId,
        collapsed: false,
        saveOnWindowClose: true,
      });
      if (!folder) throw new Error("Zen n'a pas pu créer un dossier.");
      if (parent) parent.appendChild(folder);
      report.folders++;
      makePinnedNodes(node.children, workspaceId, folder);
    } else {
      const tab = makeTab(node, workspaceId, true);
      if (tab) {
        if (parent) parent.addTabs([tab]);
        report.pinned_tabs++;
      }
    }
  }
};

for (const { source, target } of spaces) {
  makePinnedNodes(source.bookmarks, target.uuid);
  for (const node of walkLeaves(source.tabs)) {
    if (makeTab(node, target.uuid, false)) report.open_tabs++;
  }
}
for (const node of walkLeaves(sidebar.favorites)) {
  if (makeTab(node, spaces[0]?.target.uuid, true, true)) report.favorites++;
}


const bookmarkRoot = await PlacesUtils.bookmarks.insert({
  parentGuid: PlacesUtils.bookmarks.unfiledGuid,
  type: PlacesUtils.bookmarks.TYPE_FOLDER,
  title: "Arc importé",
});
const bookmarkNodes = async (nodes, parentGuid, invalidReason = null) => {
  for (const node of nodes || []) {
    if (Array.isArray(node?.children)) {
      const folder = await PlacesUtils.bookmarks.insert({
        parentGuid,
        type: PlacesUtils.bookmarks.TYPE_FOLDER,
        title: cleanTitle(node.title, "Dossier Arc"),
      });
      await bookmarkNodes(node.children, folder.guid, invalidReason);
    } else {
      const url = urlFor(node);
      if (!url) {


        if (invalidReason) skipped(invalidReason);
        continue;
      }
      await PlacesUtils.bookmarks.insert({
        parentGuid, type: PlacesUtils.bookmarks.TYPE_BOOKMARK,
        url, title: cleanTitle(node.title, url),
      });
      report.bookmarks++;
    }
  }
};
const bookmarkSection = async (title, nodes, parentGuid, invalidReason = null) => {
  if (!nodes?.length) return;
  const folder = await PlacesUtils.bookmarks.insert({
    parentGuid, type: PlacesUtils.bookmarks.TYPE_FOLDER, title,
  });
  await bookmarkNodes(nodes, folder.guid, invalidReason);
};
await bookmarkSection("Favoris Arc", sidebar.favorites, bookmarkRoot.guid);
await bookmarkSection(
  "Marque-pages Chromium", sidebar.extraBookmarks, bookmarkRoot.guid,
  "unsupported_or_invalid_chromium_bookmark_url"
);
for (const source of sidebar.spaces) {
  const folder = await PlacesUtils.bookmarks.insert({
    parentGuid: bookmarkRoot.guid,
    type: PlacesUtils.bookmarks.TYPE_FOLDER,
    title: cleanTitle(source.title, "Espace Arc"),
  });
  await bookmarkSection("Épinglés", source.bookmarks, folder.guid);
  await bookmarkSection("Onglets ouverts", source.tabs, folder.guid);
}

browser.tabContainer._invalidateCachedTabs();
workspaceManager.updateTabsContainers();
Services.prefs.setStringPref("arc-to-zen.bookmark-root", bookmarkRoot.guid);
Services.prefs.setBoolPref("arc-to-zen.layout-imported", true);
return report;
