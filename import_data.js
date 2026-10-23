const [kind, data] = args;
if (kind === 'cookies') {
  Services.prefs.setIntPref('network.cookie.maxNumber', Math.max(
    Services.prefs.getIntPref('network.cookie.maxNumber', 3000), data.length + 1000
  ));
  const result = {imported: 0, session: 0, partitioned: 0, rejected: 0};
  for (const cookie of data) {
    try {
      const attributes = {...cookie.originAttributes};
      if (cookie.partitionKey) {
        attributes.partitionKey = ChromeUtils.getPartitionKeyFromURL(
          cookie.partitionKey.topLevelSite,
          `${cookie.isSecure ? 'https' : 'http'}://${cookie.host.replace(/^\./, '')}/`,
          Boolean(cookie.partitionKey.hasCrossSiteAncestor)
        );
      }
      const partitioned = Boolean(attributes.partitionKey);
      const expiry = cookie.isSession ? Number.MAX_SAFE_INTEGER : cookie.expirySeconds * 1000;
      const validation = Services.cookies.add(
        cookie.host, cookie.path, cookie.name, cookie.value,
        cookie.isSecure, cookie.isHttpOnly, cookie.isSession, expiry,
        attributes, cookie.sameSite, cookie.schemeMap, partitioned
      );
      if (validation.result !== Ci.nsICookieValidation.eOK) {
        result.rejected++;
        continue;
      }
      result.imported++;
      result.session += Number(cookie.isSession);
      result.partitioned += Number(partitioned);
    } catch (_) {
      result.rejected++;
    }
  }

  const {SessionCookies} = ChromeUtils.importESModule(
    'moz-src:///browser/components/sessionstore/SessionCookies.sys.mjs'
  );
  result.saved_session = SessionCookies.collect().length;
  return result;
}
if (kind === 'logins') {
  const {LoginHelper} = ChromeUtils.importESModule('resource://gre/modules/LoginHelper.sys.mjs');
  const rows = await LoginHelper.maybeImportLogins(data.map(login => ({...login,
    usernameElement: login.usernameField, passwordElement: login.passwordField,
  })));
  const result = {};
  for (const row of rows) {
    const status = row.result || 'unknown';
    result[status] = (result[status] || 0) + 1;
  }
  result.stored = (await Services.logins.getAllLogins()).length;
  return result;
}
if (kind === 'history') {
  const {PlacesUtils} = ChromeUtils.importESModule('resource://gre/modules/PlacesUtils.sys.mjs');
  let imported = 0, rejected = 0;
  const pages = [];
  const now = Date.now();
  for (const page of data) {
    const visits = [];
    for (const visit of page.visits) {


      if (!Number.isFinite(visit.date) || visit.date * 1000 < now) {
        rejected++;
        continue;
      }
      const candidate = {date: new Date(visit.date), transition: visit.transition};
      try {
        PlacesUtils.validatePageInfo({url: page.url, title: page.title, visits: [candidate]});
        visits.push(candidate);
      } catch (_) {
        rejected++;
      }
    }
    if (visits.length) pages.push({url: page.url, title: page.title, visits});
  }
  if (pages.length) {
    let databaseRejections = 0;
    try {
      await PlacesUtils.history.insertMany(pages, () => imported++, () => databaseRejections++);
    } catch (error) {


      if (!databaseRejections || error.message !== 'No items were added to history.') throw error;
    }
    rejected += databaseRejections;
  }
  return {imported_visits: imported, rejected, visits: data.reduce((n, page) => n + page.visits.length, 0)};
}
if (kind === 'verify') {
  const {PlacesUtils} = ChromeUtils.importESModule('resource://gre/modules/PlacesUtils.sys.mjs');
  const db = await PlacesUtils.promiseDBConnection();
  const visits = await db.execute('SELECT COUNT(*) AS n FROM moz_historyvisits');
  const places = await db.execute('SELECT COUNT(*) AS n FROM moz_places WHERE visit_count > 0');
  const cookies = Array.from(Services.cookies.cookies);
  const win = Services.wm.getMostRecentWindow('navigator:browser');
  await win.gZenWorkspaces.promiseInitialized;
  return {
    cookies: cookies.length, session_cookies: cookies.filter(cookie => cookie.isSession).length,
    logins: (await Services.logins.getAllLogins()).length,
    history_visits: visits[0].getResultByName('n'), history_urls: places[0].getResultByName('n'),
    spaces: win.gZenWorkspaces.getWorkspaces().length,
    tabs: win.gBrowser.tabs.length,
    offline: Services.io.offline,
  };
}
throw new Error('Unknown import operation');
