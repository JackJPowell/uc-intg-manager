/* Integration Manager template-level JavaScript. */
(function () {
  'use strict';

  function $(id) { return document.getElementById(id); }


  const PAGE_TARGET = '#page-content';
  const SHELL_EXCLUDED_PREFIXES = ['/api/', '/static/'];

  function normalizePath(pathname) {
    if (!pathname) return '/';
    const p = pathname.replace(/\/+$/, '');
    return p || '/';
  }

  function isShellNavigationLink(anchor) {
    if (!anchor || anchor.dataset.noShell === 'true') return false;
    if (anchor.target && anchor.target !== '_self') return false;
    if (anchor.hasAttribute('download')) return false;
    const href = anchor.getAttribute('href');
    if (!href || href.startsWith('#') || href.startsWith('mailto:') || href.startsWith('tel:') || href.startsWith('javascript:')) return false;
    let url;
    try { url = new URL(href, window.location.href); } catch (_) { return false; }
    if (url.origin !== window.location.origin) return false;
    if (SHELL_EXCLUDED_PREFIXES.some(prefix => url.pathname.startsWith(prefix))) return false;
    return true;
  }

  function setDocumentTitleFromResponse(xhr) {
    const text = xhr && xhr.responseText;
    if (!text) return;
    const match = text.match(/<title[^>]*>([\s\S]*?)<\/title>/i);
    if (match && match[1]) document.title = match[1].replace(/\s+/g, ' ').trim();
  }

  function updateActiveNavigation(pathname) {
    const current = normalizePath(pathname || window.location.pathname);
    const activeClasses = ['bg-uc-primary', 'text-white'];
    const inactiveClasses = ['text-gray-700', 'dark:text-gray-300', 'hover:bg-gray-200', 'dark:hover:bg-uc-card', 'hover:text-gray-900', 'dark:hover:text-white'];
    document.querySelectorAll('[data-nav-link]').forEach(link => {
      const linkPath = normalizePath(link.dataset.navPath || new URL(link.href, window.location.href).pathname);
      const active = linkPath === current;
      link.classList.toggle('bg-uc-primary', active);
      link.classList.toggle('text-white', active);
      inactiveClasses.forEach(cls => link.classList.toggle(cls, !active));
    });
  }

  function closeMobileNavigation() {
    const sidebar = document.getElementById('mobile-sidebar');
    const overlay = document.getElementById('mobile-overlay');
    if (sidebar && !sidebar.classList.contains('-translate-x-full')) sidebar.classList.add('-translate-x-full');
    overlay?.classList.add('hidden');
  }

  const shellIslandTimers = new Map();
  const pageCache = new Map();

  function shellFetchUrl(absolute) {
    const url = new URL(absolute.href);
    url.searchParams.set('__shell', '1');
    return url.pathname + url.search + url.hash;
  }

  function refreshShellIsland(element, options = {}) {
    if (!element || !element.dataset || !element.dataset.url) return Promise.resolve(false);
    if (element.dataset.loading === 'true') return Promise.resolve(false);
    element.dataset.loading = 'true';
    return fetch(element.dataset.url, {
      method: 'GET',
      headers: {
        'HX-Request': 'true',
        'X-Shell-Island': element.dataset.shellIsland || element.id || 'island'
      },
      credentials: 'same-origin'
    }).then(async response => {
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const html = await response.text();
      if (element.innerHTML !== html) {
        element.innerHTML = html;
        if (window.htmx) htmx.process(element);
        if (window.initPartialBehaviors) window.initPartialBehaviors(element);
      }
      return true;
    }).catch(error => {
      console.debug('Shell island refresh failed:', element.id || element.dataset.shellIsland, error);
      return false;
    }).finally(() => {
      delete element.dataset.loading;
    });
  }

  function refreshShellIslands(options = {}) {
    document.querySelectorAll('[data-shell-island][data-url]').forEach(element => {
      refreshShellIsland(element, options);
    });
  }
  window.refreshShellIslands = refreshShellIslands;

  function initShellIslands() {
    document.querySelectorAll('[data-shell-island][data-url]').forEach(element => {
      const key = element.dataset.shellIsland || element.id || element.dataset.url;
      refreshShellIsland(element, { force: true });
      const interval = parseInt(element.dataset.interval || '0', 10);
      if (interval > 0 && !shellIslandTimers.has(key)) {
        shellIslandTimers.set(key, window.setInterval(() => {
          const current = document.querySelector(`[data-shell-island="${CSS.escape(key)}"]`) || document.getElementById(key);
          if (current) refreshShellIsland(current);
        }, interval));
      }
    });
  }

  function loadShellPage(url, options = {}) {
    const target = document.querySelector(PAGE_TARGET);
    if (!target || !window.htmx) {
      window.location.assign(url);
      return Promise.resolve(false);
    }
    const absolute = new URL(url, window.location.href);
    target.setAttribute('aria-busy', 'true');
    document.body.classList.add('shell-navigation-in-flight');
    const cacheKey = absolute.pathname + absolute.search;
    const cached = options.cache !== false ? pageCache.get(cacheKey) : null;
    const requestPromise = cached
      ? Promise.resolve(cached)
      : fetch(shellFetchUrl(absolute), {
          method: 'GET',
          headers: {
            'HX-Request': 'true',
            'X-Shell-Navigation': 'true'
          },
          credentials: 'same-origin'
        }).then(async response => {
          if (!response.ok) throw new Error(`HTTP ${response.status}`);
          const html = await response.text();
          pageCache.set(cacheKey, html);
          return html;
        });

    return requestPromise.then(async html => {
      const doc = new DOMParser().parseFromString(html, 'text/html');
      const incoming = doc.querySelector(PAGE_TARGET);
      if (!incoming) throw new Error('Response did not contain #page-content');
      target.innerHTML = incoming.innerHTML;
      const incomingTitle = doc.querySelector('title');
      if (incomingTitle) document.title = incomingTitle.textContent.trim();
      if (options.push !== false) history.pushState({ htmxShell: true }, '', absolute.pathname + absolute.search + absolute.hash);
      updateActiveNavigation(absolute.pathname);
      closeMobileNavigation();
      if (window.htmx) htmx.process(target);
      document.body.dispatchEvent(new CustomEvent('shell:page-loaded', { detail: { path: absolute.pathname, target } }));
      return true;
    }).catch(error => {
      console.error('Shell navigation failed, falling back to full navigation:', error);
      window.location.assign(absolute.href);
      return false;
    }).finally(() => {
      target.removeAttribute('aria-busy');
      document.body.classList.remove('shell-navigation-in-flight');
    });
  }

  window.loadShellPage = loadShellPage;
  window.refreshCurrentPageContent = function (options = {}) {
    return loadShellPage(window.location.pathname + window.location.search, {
      push: false,
      refreshNav: options.refreshNav !== false,
      cache: options.cache !== false
    });
  };

  document.addEventListener('click', function (event) {
    const anchor = event.target.closest && event.target.closest('a[href]');
    if (!isShellNavigationLink(anchor)) return;
    if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    loadShellPage(anchor.href);
  });

  window.addEventListener('popstate', function () {
    loadShellPage(window.location.pathname + window.location.search + window.location.hash, { push: false });
  });

  document.body.addEventListener('htmx:afterSwap', function (event) {
    if (event.target && event.target.id === 'page-content') {
      const xhr = event.detail && event.detail.xhr;
      setDocumentTitleFromResponse(xhr);
      updateActiveNavigation(window.location.pathname);
      // Persistent shell islands poll independently; do not refresh them on every page swap.
    }
  });



  document.body.addEventListener('htmx:afterRequest', function (event) {
    const method = event.detail && event.detail.requestConfig && event.detail.requestConfig.verb;
    if (method && method.toUpperCase() !== 'GET') pageCache.clear();
  });

  window.invalidateShellPageCache = function () { pageCache.clear(); };
  window.refreshCurrentPageContentUncached = function () {
    pageCache.clear();
    return window.refreshCurrentPageContent({ cache: false, refreshNav: true });
  };

  document.addEventListener('DOMContentLoaded', function () {
    initShellIslands();
    updateActiveNavigation(window.location.pathname);
  });

  function applyTheme() {
    const themeOverride = localStorage.getItem('theme');
    if (themeOverride === 'dark') document.documentElement.classList.add('dark');
    else if (themeOverride === 'light') document.documentElement.classList.remove('dark');
    else {
      const prefersDark = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
      document.documentElement.classList.toggle('dark', prefersDark);
    }
  }

  function updateThemeButtons() {
    const theme = localStorage.getItem('theme') || 'system';
    document.querySelectorAll('.theme-option').forEach(button => {
      button.classList.remove('border-blue-500', 'bg-blue-50', 'dark:bg-blue-900/30');
      button.classList.add('border-transparent');
    });
    const activeButton = $(`theme-${theme}`);
    activeButton?.classList.remove('border-transparent');
    activeButton?.classList.add('border-blue-500', 'bg-blue-50', 'dark:bg-blue-900/30');
  }

  function refreshThemeUI() { applyTheme(); updateThemeButtons(); }

  window.setTheme = function (theme) {
    if (theme === 'system') localStorage.removeItem('theme');
    else localStorage.setItem('theme', theme);
    refreshThemeUI();
    window.dispatchEvent(new CustomEvent('themeChanged', { detail: { theme } }));
  };

  applyTheme();
  if (window.matchMedia) {
    window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
      if (!localStorage.getItem('theme')) refreshThemeUI();
    });
  }
  window.addEventListener('themeChanged', updateThemeButtons);
  document.addEventListener('DOMContentLoaded', updateThemeButtons);

  window.toggleMobileMenu = function () {
    $('mobile-sidebar')?.classList.toggle('-translate-x-full');
    $('mobile-overlay')?.classList.toggle('hidden');
    $('menu-icon')?.classList.toggle('hidden');
    $('close-icon')?.classList.toggle('hidden');
  };

  window.toggleDropdown = function (event, dropdownId, forceClose = false) {
    event?.stopPropagation();
    const dropdown = $(dropdownId);
    if (!dropdown) return;
    const chevron = $(dropdownId.replace('dropdown-', 'chevron-'));
    if (forceClose || !dropdown.classList.contains('hidden')) {
      dropdown.classList.add('hidden');
      chevron?.classList.remove('fa-rotate-180');
      return;
    }
    document.querySelectorAll('[id^="dropdown-"]').forEach(d => {
      if (d.id !== dropdownId) {
        d.classList.add('hidden');
        $(d.id.replace('dropdown-', 'chevron-'))?.classList.remove('fa-rotate-180');
      }
    });
    dropdown.classList.remove('hidden');
    chevron?.classList.add('fa-rotate-180');
  };

  document.addEventListener('click', function (event) {
    if (!event.target.closest('[id^="dropdown-"]') && !event.target.closest('button')) {
      document.querySelectorAll('[id^="dropdown-"]').forEach(d => d.classList.add('hidden'));
      document.querySelectorAll('[id^="chevron-"]').forEach(c => c.classList.remove('fa-rotate-180'));
    }
  });

  document.addEventListener('DOMContentLoaded', function () {
    const quotes = [
      "Macros are just good manners", "HDMI-CEC works perfectly. Until it doesn't", "Smart TVs get dumb updates",
      "Local control beats cloud convenience", "Automation is empathy", "Convenience shouldn't need an account",
      "If it's smart, it should be quiet", "Cloud outages shouldn't turn off your lights", "The network is the product",
      "Firmware updates are a trust exercise", "Every workaround becomes permanent", "Offline is a feature",
      "Local control is peace of mind", "Automation should work without the internet", "Deterministic beats magical",
      "If it works offline, it works", "State matters", "Automation is about trust", "Inputs should never surprise you",
      "Power state is not a suggestion", "Volume is not a number, it's a feeling", "HDMI is a negotiated settlement",
      "Good macros feel invisible", "HDMI is adversarial", "CEC remembers nothing", "CEC is a suggestion",
      "CEC breaks macros", "Manual control is the fallback", "The LAN is home", "CEC never read the spec",
      "CEC needs a reboot", "The cloud always picks the worst moment", "Firmware updates ship hope",
      "Everything works until it doesn't", "Magic is just undocumented behavior", "Abstractions leak at movie night",
      "State drift is inevitable", "AV gear lies politely", "Power order matters", "Sync is always off by one",
      "It worked yesterday", "This should have been local", "It's always DNS", "HDMI is non-deterministic", "The spec allows this"
    ];
    const quoteEl = $('footer-quote');
    if (quoteEl) quoteEl.textContent = quotes[Math.floor(Math.random() * quotes.length)];
  });

  window.filterAvailableIntegrations = function () {
    const searchEl = $('available-search');
    const catEl = $('available-category-filter');
    const statusEl = $('available-status-filter');
    if (!searchEl || !catEl || !statusEl) return;
    const searchTerm = searchEl.value.toLowerCase();
    const categoryFilter = catEl.value;
    const statusFilter = statusEl.value;
    document.querySelectorAll('#available-container .integration-card').forEach(card => {
      const name = (card.dataset.name || '').toLowerCase();
      const description = (card.dataset.description || '').toLowerCase();
      const developer = (card.dataset.developer || '').toLowerCase();
      const categories = (card.dataset.categories || '').toLowerCase();
      const isInstalled = card.dataset.installed === 'true';
      const hasUpdate = card.dataset.hasUpdate === 'true';
      const supportsBackup = card.dataset.supportsBackup === 'true';
      const matchesSearch = !searchTerm || name.includes(searchTerm) || description.includes(searchTerm) || developer.includes(searchTerm) || categories.includes(searchTerm);
      const matchesCategory = categoryFilter === 'all' || categories.includes(categoryFilter);
      let matchesStatus = true;
      if (statusFilter === 'installed') matchesStatus = isInstalled;
      else if (statusFilter === 'not-installed') matchesStatus = !isInstalled;
      else if (statusFilter === 'updates') matchesStatus = hasUpdate;
      else if (statusFilter === 'supports-backup') matchesStatus = supportsBackup;
      card.style.display = (matchesSearch && matchesCategory && matchesStatus) ? '' : 'none';
    });
  };

  window.toggleSortReverse = function () {
    const reverseFlag = $('sort-reverse-flag');
    const icon = $('sort-reverse-icon');
    if (!reverseFlag) return;
    const next = reverseFlag.value === 'true' ? 'false' : 'true';
    reverseFlag.value = next;
    icon?.classList.toggle('fa-arrow-down-wide-short', next !== 'true');
    icon?.classList.toggle('fa-arrow-up-wide-short', next === 'true');
    if (window.htmx) {
      htmx.ajax('POST', '/api/settings/sort', {
        target: '#available-container', swap: 'innerHTML', values: { sort_by: $('available-sort-by')?.value || 'original', sort_reverse: next }
      });
    }
  };

  window.filterInstalledIntegrations = function () {
    const searchEl = $('intg-search');
    const statusEl = $('intg-status-filter');
    if (!searchEl || !statusEl) return;
    const searchTerm = searchEl.value.toLowerCase();
    const statusFilter = statusEl.value;
    document.querySelectorAll('#integrations-container .integration-card').forEach(card => {
      const name = (card.dataset.name || '').toLowerCase();
      const description = (card.dataset.description || '').toLowerCase();
      const developer = (card.dataset.developer || '').toLowerCase();
      const status = (card.dataset.status || '').toLowerCase();
      const hasUpdate = card.dataset.hasUpdate === 'true';
      const matchesSearch = !searchTerm || name.includes(searchTerm) || description.includes(searchTerm) || developer.includes(searchTerm);
      let matchesStatus = true;
      if (statusFilter === 'updates') matchesStatus = hasUpdate;
      else if (statusFilter === 'needs-config') matchesStatus = status === 'not_configured';
      else if (statusFilter === 'connected') matchesStatus = status === 'connected' || status === 'ok';
      else if (statusFilter === 'disconnected') matchesStatus = status === 'disconnected' || status === 'error';
      card.style.display = (matchesSearch && matchesStatus) ? '' : 'none';
    });
  };

  function spinRefreshIcon(event, spinning) {
    const refreshIcon = $('refresh-icon');
    if (refreshIcon && event.target?.querySelector?.('#refresh-icon')) refreshIcon.classList.toggle('fa-spin', spinning);
  }
  document.body.addEventListener('htmx:beforeRequest', e => spinRefreshIcon(e, true));
  document.body.addEventListener('htmx:afterRequest', e => spinRefreshIcon(e, false));

  window.checkForFirmwareUpdate = async function () {
    const btn = $('firmware-check-btn');
    const icon = $('firmware-check-icon');
    if (!btn) return;
    btn.disabled = true;
    icon?.classList.add('fa-spin');
    try {
      const resp = await fetch('/api/diagnostics/system-update-check', { method: 'POST' });
      const data = await resp.json();
      $('firmware-installed') && ($('firmware-installed').textContent = data.installed_version || '—');
      if (window.refreshCurrentPageContent) window.refreshCurrentPageContent({ refreshNav: true });
    } catch (e) {
      alert('Failed to check firmware update: ' + e);
    } finally {
      btn.disabled = false;
      icon?.classList.remove('fa-spin');
    }
  };

  window.toggleSection = function (sectionId) {
    $(sectionId + '-body')?.classList.toggle('hidden');
    $(sectionId + '-chevron')?.classList.toggle('rotate-180');
  };

  async function postSystemAction(url) {
    const statusEl = $('system-control-status');
    if (statusEl) statusEl.textContent = 'Sending command…';
    try {
      const resp = await fetch(url, { method: 'POST' });
      const data = await resp.json().catch(() => ({}));
      if (statusEl) statusEl.textContent = data.message || (resp.ok ? 'Command sent.' : 'Command failed.');
    } catch (e) {
      if (statusEl) statusEl.textContent = 'Command failed: ' + e;
    }
  }
  window.restartRemote = () => postSystemAction('/api/system/restart');
  window.rebootRemote = () => postSystemAction('/api/system/reboot');

  window.getSelectedServices = function () {
    return Array.from(document.querySelectorAll('input[name="service-checkbox"]:checked')).map(cb => cb.value);
  };
  window.onServiceChange = function () {
    const selected = window.getSelectedServices();
    const label = $('service-dropdown-label');
    if (label) label.textContent = selected.length ? `${selected.length} service${selected.length === 1 ? '' : 's'} selected` : 'Select services...';
    const all = $('service-select-all');
    const boxes = document.querySelectorAll('input[name="service-checkbox"]');
    if (all) all.checked = boxes.length > 0 && selected.length === boxes.length;
  };
  window.toggleAllServices = function (source) {
    document.querySelectorAll('input[name="service-checkbox"]').forEach(cb => { cb.checked = source.checked; });
    window.onServiceChange();
  };
  window.toggleServiceDropdown = function () {
    $('service-dropdown-panel')?.classList.toggle('hidden');
    $('service-dropdown-chevron')?.classList.toggle('rotate-180');
    $('priority-dropdown-panel')?.classList.add('hidden');
    $('priority-dropdown-chevron')?.classList.remove('rotate-180');
  };
  window.togglePriorityDropdown = function () {
    $('priority-dropdown-panel')?.classList.toggle('hidden');
    $('priority-dropdown-chevron')?.classList.toggle('rotate-180');
    $('service-dropdown-panel')?.classList.add('hidden');
    $('service-dropdown-chevron')?.classList.remove('rotate-180');
  };
  window.selectPriority = function (value, label) {
    if ($('priority-select')) $('priority-select').value = value;
    if ($('priority-dropdown-label')) $('priority-dropdown-label').textContent = label;
    document.querySelectorAll('.priority-option').forEach(o => o.classList.remove('bg-gray-100', 'dark:bg-gray-600', 'font-medium'));
    document.querySelector(`.priority-option[data-value="${value}"]`)?.classList.add('bg-gray-100', 'dark:bg-gray-600', 'font-medium');
    $('priority-dropdown-panel')?.classList.add('hidden');
    $('priority-dropdown-chevron')?.classList.remove('rotate-180');
  };
  function setButtonBusy(buttonId, iconId, _spinnerId, busy) {
    const button = $(buttonId);
    const icon = $(iconId);
    if (button) button.disabled = !!busy;
    icon?.classList.toggle('fa-spin', !!busy);
  }

  window.buildIntegrationLogParams = function () {
    const services = window.getSelectedServices();
    const priority = $('priority-select')?.value || '7';
    const params = new URLSearchParams({ priority });
    if (services.length) params.set('service', services.join(','));
    return params;
  };

  window.fetchLogs = async function () {
    const target = $('integration-log-entries');
    if (!target) return;
    const params = window.buildIntegrationLogParams();
    setButtonBusy('refresh-btn', 'integration-log-refresh-icon', null, true);
    try {
      const response = await fetch(`/api/integration-logs/entries?${params.toString()}`, {
        headers: { 'HX-Request': 'true' },
        credentials: 'same-origin'
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      target.innerHTML = await response.text();
      if (window.htmx) htmx.process(target);
    } catch (error) {
      target.innerHTML = `<div class="p-8 text-center text-red-500">Failed to fetch integration logs: ${String(error)}</div>`;
    } finally {
      setButtonBusy('refresh-btn', 'integration-log-refresh-icon', null, false);
    }
  };

  window.downloadLogs = async function () {
    const params = window.buildIntegrationLogParams();
    if (!params.get('service')) {
      alert('Select at least one integration service before downloading logs.');
      return;
    }
    setButtonBusy('download-btn', 'integration-log-download-icon', null, true);
    try {
      const response = await fetch(`/api/integration-logs/download?${params.toString()}`, {
        headers: { 'HX-Request': 'true' },
        credentials: 'same-origin'
      });
      if (!response.ok) throw new Error(await response.text() || `HTTP ${response.status}`);
      const blob = await response.blob();
      const disposition = response.headers.get('Content-Disposition') || '';
      const filenameMatch = disposition.match(/filename="?([^";]+)"?/i);
      const filename = filenameMatch ? filenameMatch[1] : 'integration_logs.txt';
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = filename;
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
    } catch (error) {
      alert('Failed to download logs: ' + String(error));
    } finally {
      setButtonBusy('download-btn', 'integration-log-download-icon', null, false);
    }
  };

  function initLogsPages() {
    const managerAuto = $('auto-refresh');
    if (managerAuto && $('log-entries') && !managerAuto.dataset.bound) {
      managerAuto.dataset.bound = 'true';
      let refreshInterval = null;
      managerAuto.addEventListener('change', function () {
        if (this.checked) refreshInterval = setInterval(() => htmx.ajax('GET', '/api/logs/entries', '#log-entries'), 10000);
        else if (refreshInterval) { clearInterval(refreshInterval); refreshInterval = null; }
      });
    }
    const integrationAuto = $('auto-refresh-logs') || ($('integration-log-entries') ? $('auto-refresh') : null);
    if (integrationAuto && $('integration-log-entries') && !integrationAuto.dataset.bound) {
      integrationAuto.dataset.bound = 'true';
      let interval = null;
      integrationAuto.addEventListener('change', function () {
        if (this.checked) interval = setInterval(window.fetchLogs, 10000);
        else if (interval) { clearInterval(interval); interval = null; }
      });
    }
  }

  window.uploadBackupFile = function (input) {
    const file = input.files[0];
    if (!file) return;
    const formData = new FormData();
    formData.append('file', file);
    fetch('/api/backups/upload', { method: 'POST', body: formData })
      .then(r => r.json())
      .then(data => {
        const resultDiv = $('backup-result');
        if (resultDiv) resultDiv.innerHTML = data.status === 'ok' ? `<div class="text-green-400">${data.message}</div>` : `<div class="text-red-400">Error: ${data.message}</div>`;
        if (data.status === 'ok' && window.htmx) htmx.trigger('#backup-list', 'load');
        input.value = '';
      })
      .catch(error => {
        if ($('backup-result')) $('backup-result').innerHTML = `<div class="text-red-400">Upload failed: ${error}</div>`;
        input.value = '';
      });
  };

  window.checkLockStatus = async function () {
    const btn = $('resume-ops-btn');
    const hint = $('resume-ops-hint');
    if (!btn || !hint) return;
    try {
      const resp = await fetch('/api/operation-lock/status');
      const data = await resp.json();
      if (data.locked) {
        btn.disabled = false;
        const mins = data.elapsed_seconds !== null ? Math.floor(data.elapsed_seconds / 60) : null;
        hint.textContent = mins !== null ? `An operation has been stuck for about ${mins} minute${mins !== 1 ? 's' : ''}. Click to unblock installs and updates.` : 'An operation appears to be stuck. Click to unblock installs and updates.';
        hint.classList.add('text-yellow-600', 'dark:text-yellow-400');
        hint.classList.remove('text-gray-500', 'dark:text-gray-400');
      } else {
        btn.disabled = true;
        hint.textContent = 'No stuck operations detected.';
        hint.classList.remove('text-yellow-600', 'dark:text-yellow-400');
        hint.classList.add('text-gray-500', 'dark:text-gray-400');
      }
    } catch (_) { hint.textContent = 'Could not check status.'; }
  };
  window.resumeOperations = async function () {
    const btn = $('resume-ops-btn');
    const result = $('resume-ops-result');
    if (!btn || !result) return;
    btn.disabled = true;
    try {
      const resp = await fetch('/api/operation-lock/release', { method: 'POST' });
      const data = await resp.json();
      result.innerHTML = data.status === 'ok' && data.was_locked ? '<span class="text-green-600 dark:text-green-400"><i class="fa-solid fa-circle-check mr-1"></i>Operations unblocked.</span>' : '<span class="text-gray-500 dark:text-gray-400">No lock was active.</span>';
    } catch (_) { result.innerHTML = '<span class="text-red-500">Failed to release lock.</span>'; }
    await window.checkLockStatus();
  };

  function initSystemMessages() {
    const refreshBtn = $('refreshBtn');
    if (!refreshBtn || refreshBtn.dataset.bound) return;
    refreshBtn.dataset.bound = 'true';
    document.body.addEventListener('htmx:beforeRequest', function (event) {
      if (event.detail.elt?.id === 'refreshBtn') refreshBtn.disabled = true;
    });
    document.body.addEventListener('htmx:afterRequest', function (event) {
      if (event.detail.elt?.id === 'refreshBtn') {
        refreshBtn.disabled = false;
        if (event.detail.xhr.status === 200) {
          if (window.invalidateShellPageCache) window.invalidateShellPageCache();
          if (window.refreshShellIslands) window.refreshShellIslands({ force: true });
          if (window.refreshCurrentPageContent) window.refreshCurrentPageContent({refreshNav: true, cache: false}); else location.reload();
        } else {
          alert('Failed to refresh messages from GitHub. Please try again later.');
        }
      }
    });
  }

  function initUpdatingPage() {
    const pingTarget = $('ping-target');
    if (!pingTarget || pingTarget.dataset.bound) return;
    pingTarget.dataset.bound = 'true';
    const inplace = pingTarget.dataset.inplace === 'true';
    setTimeout(function () {
      pingTarget.setAttribute('hx-get', '/health');
      pingTarget.setAttribute('hx-trigger', 'every 3s');
      pingTarget.setAttribute('hx-swap', 'none');
      pingTarget.setAttribute('hx-on::after-request', "if(event.detail.xhr.status === 200 && event.detail.xhr.responseText.trim() === 'OK') { if(window.loadShellPage){ window.loadShellPage('/', {push:false, refreshNav:true}); } else { window.location.href = '/'; } }");
      if (window.htmx) htmx.process(pingTarget);
    }, inplace ? 3000 : 15000);
    setTimeout(() => $('fallback-msg')?.classList.remove('hidden'), 90000);
  }

  function initNotifications() {
    const notificationRoot = $('notification-message');
    if (!notificationRoot || notificationRoot.dataset.notificationsBound === 'true') return;
    notificationRoot.dataset.notificationsBound = 'true';
// Helper function to show messages
function showMessage(message, isError = false) {
    const messageDiv = document.getElementById('notification-message');
    messageDiv.className = `fixed top-20 left-4 right-4 sm:left-1/2 sm:right-auto sm:transform sm:-translate-x-1/2 z-[9999] sm:max-w-md sm:w-full shadow-2xl transition-all duration-300 p-4 rounded-lg ${isError ? 'bg-red-600 border border-red-500 text-white' : 'bg-green-600 border border-green-500 text-white'}`;
    messageDiv.innerHTML = `<i class="fa-solid fa-${isError ? 'exclamation-circle' : 'check-circle'} mr-2"></i>${message}`;
    messageDiv.classList.remove('hidden');
    
    // Auto-hide after 4 seconds with fade out
    setTimeout(() => {
        messageDiv.style.opacity = '0';
        setTimeout(() => {
            messageDiv.classList.add('hidden');
            messageDiv.style.opacity = '1';
        }, 300);
    }, 4000);
}

// Auto-save when toggles change
document.getElementById('ha-enabled').addEventListener('change', async (e) => {
    const formData = new FormData(document.getElementById('ha-form'));
    try {
        const response = await fetch('/api/notifications/home-assistant', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                enabled: e.target.checked,
                url: formData.get('url'),
                token: formData.get('token')
            })
        });
        if (response.ok) {
            showMessage(e.target.checked ? 'Home Assistant notifications enabled' : 'Home Assistant notifications disabled');
        }
    } catch (error) {
        showMessage('Error updating settings: ' + error.message, true);
    }
});

document.getElementById('webhook-enabled').addEventListener('change', async (e) => {
    const formData = new FormData(document.getElementById('webhook-form'));
    let headers = null;
    const headersText = formData.get('headers').trim();
    if (headersText) {
        try {
            headers = JSON.parse(headersText);
        } catch (error) {
            headers = null;
        }
    }
    
    try {
        const response = await fetch('/api/notifications/webhook', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                enabled: e.target.checked,
                url: formData.get('url'),
                headers: headers
            })
        });
        if (response.ok) {
            showMessage(e.target.checked ? 'Webhook notifications enabled' : 'Webhook notifications disabled');
        }
    } catch (error) {
        showMessage('Error updating settings: ' + error.message, true);
    }
});

document.getElementById('pushover-enabled').addEventListener('change', async (e) => {
    const formData = new FormData(document.getElementById('pushover-form'));
    try {
        const response = await fetch('/api/notifications/pushover', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                enabled: e.target.checked,
                user_key: formData.get('user_key'),
                app_token: formData.get('app_token')
            })
        });
        if (response.ok) {
            showMessage(e.target.checked ? 'Pushover notifications enabled' : 'Pushover notifications disabled');
        }
    } catch (error) {
        showMessage('Error updating settings: ' + error.message, true);
    }
});

document.getElementById('ntfy-enabled').addEventListener('change', async (e) => {
    const formData = new FormData(document.getElementById('ntfy-form'));
    try {
        const response = await fetch('/api/notifications/ntfy', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                enabled: e.target.checked,
                server: formData.get('server'),
                topic: formData.get('topic'),
                token: formData.get('token')
            })
        });
        if (response.ok) {
            showMessage(e.target.checked ? 'ntfy notifications enabled' : 'ntfy notifications disabled');
        }
    } catch (error) {
        showMessage('Error updating settings: ' + error.message, true);
    }
});

document.getElementById('discord-enabled').addEventListener('change', async (e) => {
    const formData = new FormData(document.getElementById('discord-form'));
    try {
        const response = await fetch('/api/notifications/discord', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                enabled: e.target.checked,
                webhook_url: formData.get('webhook_url')
            })
        });
        if (response.ok) {
            showMessage(e.target.checked ? 'Discord notifications enabled' : 'Discord notifications disabled');
        }
    } catch (error) {
        showMessage('Error updating settings: ' + error.message, true);
    }
});

// Refresh HA services function (reusable)
async function refreshHAServices() {
    const statusEl = document.getElementById('ha-service-status');
    const refreshBtn = document.getElementById('ha-refresh-services');
    const icon = refreshBtn?.querySelector('i');
    
    // Show loading state
    if (icon) icon.classList.add('fa-spin');
    if (statusEl) {
        statusEl.textContent = 'Fetching services...';
        statusEl.className = 'italic text-gray-500';
    }
    
    try {
        const response = await fetch('/api/notifications/home-assistant/services');
        const data = await response.json();
        
        if (data.success && data.services) {
            const select = document.getElementById('ha-service');
            const currentValue = select.value;
            
            // Clear and rebuild options
            select.innerHTML = '<option value="notify">notify (broadcast - default)</option>';
            
            // Add fetched services
            data.services.forEach(service => {
                const option = document.createElement('option');
                option.value = service;
                option.textContent = service;
                if (service === currentValue) {
                    option.selected = true;
                }
                select.appendChild(option);
            });
            
            // If current value isn't in the list, add it
            if (currentValue && currentValue !== 'notify' && !data.services.includes(currentValue)) {
                const option = document.createElement('option');
                option.value = currentValue;
                option.textContent = currentValue;
                option.selected = true;
                select.appendChild(option);
            }
            
            if (statusEl) {
                statusEl.textContent = `Found ${data.services.length} service(s)`;
                statusEl.className = 'italic text-green-600 dark:text-green-400';
            }
        } else {
            if (statusEl) {
                statusEl.textContent = data.error || 'Failed to fetch services';
                statusEl.className = 'italic text-red-600 dark:text-red-400';
            }
        }
    } catch (error) {
        if (statusEl) {
            statusEl.textContent = 'Error: ' + error.message;
            statusEl.className = 'italic text-red-600 dark:text-red-400';
        }
    } finally {
        if (icon) icon.classList.remove('fa-spin');
    }
}

// Refresh HA services on button click
document.getElementById('ha-refresh-services').addEventListener('click', refreshHAServices);

// Auto-load HA services on page load if HA is configured
window.addEventListener('DOMContentLoaded', () => {
    const haUrl = document.getElementById('ha-url')?.value;
    const haToken = document.getElementById('ha-token')?.value;
    
    // Only auto-load if URL and token are configured
    if (haUrl && haToken) {
        refreshHAServices();
    }
});

// Home Assistant form
document.getElementById('ha-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const formData = new FormData(e.target);
    const enabled = document.getElementById('ha-enabled').checked;
    
    try {
        const response = await fetch('/api/notifications/home-assistant', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                enabled: enabled,
                url: formData.get('url'),
                token: formData.get('token'),
                service: formData.get('service') || 'notify'
            })
        });
        
        if (response.ok) {
            showMessage('Home Assistant settings saved successfully');
        } else {
            showMessage('Failed to save Home Assistant settings', true);
        }
    } catch (error) {
        showMessage('Error saving settings: ' + error.message, true);
    }
});

document.getElementById('ha-test').addEventListener('click', async () => {
    const formData = new FormData(document.getElementById('ha-form'));
    
    try {
        const response = await fetch('/api/notifications/home-assistant/test', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                url: formData.get('url'),
                token: formData.get('token'),
                service: formData.get('service') || 'notify'
            })
        });
        if (response.ok) {
            showMessage('Test notification sent to Home Assistant');
        } else {
            showMessage('Failed to send test notification', true);
        }
    } catch (error) {
        showMessage('Error sending test: ' + error.message, true);
    }
});

// Webhook form
document.getElementById('webhook-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const formData = new FormData(e.target);
    const enabled = document.getElementById('webhook-enabled').checked;
    
    let headers = null;
    const headersText = formData.get('headers').trim();
    if (headersText) {
        try {
            headers = JSON.parse(headersText);
        } catch (error) {
            showMessage('Invalid JSON in custom headers', true);
            return;
        }
    }
    
    try {
        const response = await fetch('/api/notifications/webhook', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                enabled: enabled,
                url: formData.get('url'),
                headers: headers
            })
        });
        
        if (response.ok) {
            showMessage('Webhook settings saved successfully');
        } else {
            showMessage('Failed to save Webhook settings', true);
        }
    } catch (error) {
        showMessage('Error saving settings: ' + error.message, true);
    }
});

document.getElementById('webhook-test').addEventListener('click', async () => {
    try {
        const response = await fetch('/api/notifications/webhook/test', {method: 'POST'});
        if (response.ok) {
            showMessage('Test notification sent via Webhook');
        } else {
            showMessage('Failed to send test notification', true);
        }
    } catch (error) {
        showMessage('Error sending test: ' + error.message, true);
    }
});

// Pushover form
document.getElementById('pushover-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const formData = new FormData(e.target);
    const enabled = document.getElementById('pushover-enabled').checked;
    
    try {
        const response = await fetch('/api/notifications/pushover', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                enabled: enabled,
                user_key: formData.get('user_key'),
                app_token: formData.get('app_token')
            })
        });
        
        if (response.ok) {
            showMessage('Pushover settings saved successfully');
        } else {
            showMessage('Failed to save Pushover settings', true);
        }
    } catch (error) {
        showMessage('Error saving settings: ' + error.message, true);
    }
});

document.getElementById('pushover-test').addEventListener('click', async () => {
    try {
        const response = await fetch('/api/notifications/pushover/test', {method: 'POST'});
        if (response.ok) {
            showMessage('Test notification sent via Pushover');
        } else {
            showMessage('Failed to send test notification', true);
        }
    } catch (error) {
        showMessage('Error sending test: ' + error.message, true);
    }
});

// ntfy form
document.getElementById('ntfy-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const formData = new FormData(e.target);
    const enabled = document.getElementById('ntfy-enabled').checked;
    
    try {
        const response = await fetch('/api/notifications/ntfy', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                enabled: enabled,
                server: formData.get('server'),
                topic: formData.get('topic'),
                token: formData.get('token')
            })
        });
        
        if (response.ok) {
            showMessage('ntfy settings saved successfully');
        } else {
            showMessage('Failed to save ntfy settings', true);
        }
    } catch (error) {
        showMessage('Error saving settings: ' + error.message, true);
    }
});

document.getElementById('ntfy-test').addEventListener('click', async () => {
    try {
        const response = await fetch('/api/notifications/ntfy/test', {method: 'POST'});
        if (response.ok) {
            showMessage('Test notification sent via ntfy');
        } else {
            showMessage('Failed to send test notification', true);
        }
    } catch (error) {
        showMessage('Error sending test: ' + error.message, true);
    }
});

// Discord form
document.getElementById('discord-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const formData = new FormData(e.target);
    const enabled = document.getElementById('discord-enabled').checked;
    
    try {
        const response = await fetch('/api/notifications/discord', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                enabled: enabled,
                webhook_url: formData.get('webhook_url')
            })
        });
        
        if (response.ok) {
            showMessage('Discord settings saved successfully');
        } else {
            showMessage('Failed to save Discord settings', true);
        }
    } catch (error) {
        showMessage('Error saving settings: ' + error.message, true);
    }
});

document.getElementById('discord-test').addEventListener('click', async () => {
    try {
        const response = await fetch('/api/notifications/discord/test', {method: 'POST'});
        if (response.ok) {
            showMessage('Test notification sent to Discord');
        } else {
            showMessage('Failed to send test notification', true);
        }
    } catch (error) {
        showMessage('Error sending test: ' + error.message, true);
    }
});

// Notification triggers form
document.getElementById('triggers-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const formData = new FormData(e.target);
    
    // Build triggers object from checkbox states
    const triggers = {};
    const checkboxes = e.target.querySelectorAll('input[type="checkbox"]');
    checkboxes.forEach(checkbox => {
        triggers[checkbox.name] = checkbox.checked;
    });
    
    try {
        const response = await fetch('/api/notifications/triggers', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(triggers)
        });
        
        if (response.ok) {
            showMessage('Notification preferences saved successfully');
        } else {
            showMessage('Failed to save notification preferences', true);
        }
    } catch (error) {
        showMessage('Error saving preferences: ' + error.message, true);
    }
});
    // DOMContentLoaded has already fired by the time initNotifications runs from this file.
    // Preserve the old auto-load behavior explicitly.
    try {
      const haUrl = document.getElementById('ha-url')?.value;
      const haToken = document.getElementById('ha-token')?.value;
      if (haUrl && haToken && typeof refreshHAServices === 'function') refreshHAServices();
    } catch (_) {}
  }

  document.body.addEventListener('shell:page-loaded', function () {
    initLogsPages();
    initNotifications();
    initSystemMessages();
    initUpdatingPage();
    if (window.checkLockStatus) window.checkLockStatus();
  });

  document.addEventListener('DOMContentLoaded', function () {
    initLogsPages();
    initNotifications();
    window.checkLockStatus();
    initSystemMessages();
    initUpdatingPage();
  });
})();
