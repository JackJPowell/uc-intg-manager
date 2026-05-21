/* Integration Manager partial-level JavaScript. */
(function () {
  'use strict';

  function $(id) { return document.getElementById(id); }

// Open modal with optional title and content
    function openModal(title = 'Modal', content = null) {
        const overlay = document.getElementById('modal-overlay');
        const container = document.getElementById('modal-container');
        const titleElement = document.getElementById('modal-title');
        const contentElement = document.getElementById('modal-content');
        
        // Set title
        titleElement.textContent = title;
        
        // Set content if provided, otherwise show loading spinner
        if (content) {
            contentElement.innerHTML = content;
        } else {
            contentElement.innerHTML = `
                <div class="flex items-center justify-center py-12">
                    <svg class="animate-spin h-8 w-8 text-uc-primary" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24">
                        <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
                        <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
                    </svg>
                </div>
            `;
        }
        
        // Show overlay
        overlay.classList.remove('hidden');
        
        // Trigger animation after a brief delay
        setTimeout(() => {
            overlay.classList.add('opacity-100');
            container.classList.remove('scale-95', 'opacity-0');
            container.classList.add('scale-100', 'opacity-100');
        }, 10);
        
        // Prevent body scroll
        document.body.style.overflow = 'hidden';
        
        // Add escape key listener
        document.addEventListener('keydown', handleEscapeKey);
    }
    
    // Close modal with animation
    function closeModal(event) {
        // If event is provided and it's not from clicking the overlay, ignore
        if (event && event.target.id !== 'modal-overlay') {
            return;
        }
        
        const overlay = document.getElementById('modal-overlay');
        const container = document.getElementById('modal-container');
        
        // Trigger close animation
        overlay.classList.remove('opacity-100');
        container.classList.remove('scale-100', 'opacity-100');
        container.classList.add('scale-95', 'opacity-0');
        
        // Hide after animation
        setTimeout(() => {
            overlay.classList.add('hidden');
            // Clear content
            document.getElementById('modal-content').innerHTML = '';
            // Reset footer
            document.getElementById('modal-footer').classList.add('hidden');
        }, 300);
        
        // Restore body scroll
        document.body.style.overflow = '';
        
        // Remove escape key listener
        document.removeEventListener('keydown', handleEscapeKey);
    }
    
    // Handle escape key
    function handleEscapeKey(event) {
        if (event.key === 'Escape') {
            closeModal();
        }
    }
    
    // Update modal content (useful for HTMX responses)
    function updateModalContent(content) {
        document.getElementById('modal-content').innerHTML = content;
    }
    
    // Update modal title
    function updateModalTitle(title) {
        document.getElementById('modal-title').textContent = title;
    }
    
    // Show modal footer
    function showModalFooter(content) {
        const footer = document.getElementById('modal-footer');
        footer.innerHTML = content;
        footer.classList.remove('hidden');
    }
    
    // Hide modal footer
    function hideModalFooter() {
        document.getElementById('modal-footer').classList.add('hidden');
    }
    
    // Listen for HTMX afterSwap to update modal title if data attribute exists
    document.body.addEventListener('htmx:afterSwap', function(event) {
        // Check if the swap target was the modal content
        if (event.target && event.target.id === 'modal-content') {
            // Look for data-modal-title attribute in the swapped content
            const content = event.target.querySelector('[data-modal-title]');
            if (content && content.dataset.modalTitle) {
                updateModalTitle(content.dataset.modalTitle);
            }
        }
    });
  window.openModal = openModal;
  window.closeModal = closeModal;

  window.validateRemoteName = function (deviceId) {
    const input = $('remote-name-' + deviceId);
    const label = $('label-' + deviceId);
    if (!input || !label) return false;
    if (!input.value.trim()) {
      input.classList.add('border-red-500', 'focus:border-red-500');
      label.classList.add('text-red-400');
      label.innerHTML = '<i class="fa-solid fa-triangle-exclamation text-red-400 mr-2"></i>Please enter a remote name';
      return false;
    }
    input.classList.remove('border-red-500', 'focus:border-red-500');
    label.classList.remove('text-red-400');
    label.innerHTML = '<i class="fa-solid fa-wave-square text-blue-400 mr-2"></i>Re-associate with new remote:';
    return true;
  };

  function initRemoteSelector(root = document) {
    const scope = root && root.querySelector ? root : document;
    const button = scope.querySelector('#remote-selector-button');
    const menu = scope.querySelector('#remote-selector-menu');
    if (!button || !menu || button.dataset.bound) return;
    button.dataset.bound = 'true';

    function closeThisMenu() {
      menu.classList.add('hidden');
      button.setAttribute('aria-expanded', 'false');
    }

    button.addEventListener('click', function (event) {
      event.stopPropagation();
      const isHidden = menu.classList.toggle('hidden');
      button.setAttribute('aria-expanded', isHidden ? 'false' : 'true');
    });

    scope.querySelectorAll('.remote-selector-item').forEach(item => {
      if (item.dataset.bound) return;
      item.dataset.bound = 'true';
      item.addEventListener('click', function (event) {
        event.preventDefault();
        event.stopPropagation();
        const remoteId = this.dataset.remoteId;
        const remoteName = this.textContent.trim().replace(/\s+/g, ' ');
        closeThisMenu();
        button.disabled = true;

        // The desktop and mobile selectors can both exist in the DOM. Keep their labels in sync.
        document.querySelectorAll('#selected-remote-name').forEach(label => {
          if (remoteName) label.textContent = remoteName;
        });

        fetch('/api/active-remote', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'HX-Request': 'true' },
          credentials: 'same-origin',
          body: JSON.stringify({ remote_id: remoteId })
        })
          .then(async response => {
            const data = await response.json().catch(() => ({}));
            if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
            return data;
          })
          .then(data => {
            if (data.status === 'ok') {
              const canonicalRemoteId = data.active_remote_id || remoteId;
              localStorage.setItem('active_remote_id', canonicalRemoteId);
              if (window.invalidateShellPageCache) window.invalidateShellPageCache();
              const refreshIslands = window.refreshShellIslands ? Promise.resolve(window.refreshShellIslands({ force: true })) : Promise.resolve();
              refreshIslands.finally(() => {
                if (window.refreshCurrentPageContent) {
                  window.refreshCurrentPageContent({ refreshNav: true, cache: false });
                } else {
                  window.location.assign(window.location.href);
                }
              });
            } else {
              throw new Error(data.error || 'Unknown error');
            }
          })
          .catch(error => {
            console.error('Error switching remote:', error);
            alert('Error switching remote: ' + error.message);
          })
          .finally(() => {
            button.disabled = false;
          });
      });
    });
  }

  document.addEventListener('click', function (event) {
    document.querySelectorAll('#remote-selector-menu').forEach(menu => {
      const container = menu.closest('[data-shell-island]') || menu.parentElement;
      const button = container ? container.querySelector('#remote-selector-button') : null;
      if (button && !button.contains(event.target) && !menu.contains(event.target)) {
        menu.classList.add('hidden');
        button.setAttribute('aria-expanded', 'false');
      }
    });
  });

  function syncStoredRemote() {
    const storedRemoteId = localStorage.getItem('active_remote_id');
    if (!storedRemoteId || window.__remoteSyncDone) return;
    window.__remoteSyncDone = true;
    fetch('/api/active-remote')
      .then(response => response.json())
      .then(data => {
        const activeRemoteId = data.active_remote_id || data.id;
        if (activeRemoteId !== storedRemoteId) {
          return fetch('/api/active-remote', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'HX-Request': 'true' },
            credentials: 'same-origin',
            body: JSON.stringify({ remote_id: storedRemoteId })
          }).then(() => {
            if (window.invalidateShellPageCache) window.invalidateShellPageCache();
            if (window.refreshShellIslands) window.refreshShellIslands({ force: true });
            if (window.refreshCurrentPageContent) window.refreshCurrentPageContent({refreshNav: true, cache: false});
            else window.location.assign(window.location.href);
          });
        }
      })
      .catch(error => console.debug('Remote sync skipped:', error));
  }

  function compareVersions(v1, v2) {
    const splitId = (id) => {
      const m = /^([^\d]*)(\d+)$/.exec(id);
      if (m) return m[1] ? [m[1], parseInt(m[2], 10)] : [parseInt(m[2], 10)];
      return [id];
    };
    const INLINE_SUFFIX = /^(\d+)((?:alpha|beta|preview|pre|rc|dev|a|b)\d*)$/i;
    const parse = (v) => {
      const s = String(v).replace(/^v/i, '').split('+', 1)[0];
      const dashIdx = s.indexOf('-');
      let mainPart, prePart;
      if (dashIdx >= 0) { mainPart = s.slice(0, dashIdx); prePart = s.slice(dashIdx + 1); }
      else { mainPart = s; prePart = undefined; }
      const segs = mainPart.split('.');
      if (prePart === undefined) {
        const last = segs[segs.length - 1];
        const m = INLINE_SUFFIX.exec(last);
        if (m) { segs[segs.length - 1] = m[1]; prePart = m[2]; }
      }
      return { main: segs.map(n => parseInt(n, 10) || 0), pre: prePart ? prePart.split('.').flatMap(splitId) : null };
    };
    const a = parse(v1), b = parse(v2);
    const len = Math.max(a.main.length, b.main.length);
    for (let i = 0; i < len; i++) {
      const diff = (a.main[i] || 0) - (b.main[i] || 0);
      if (diff) return diff;
    }
    if (!a.pre && !b.pre) return 0;
    if (!a.pre) return 1;
    if (!b.pre) return -1;
    const plen = Math.max(a.pre.length, b.pre.length);
    for (let i = 0; i < plen; i++) {
      const ai = a.pre[i], bi = b.pre[i];
      if (ai === undefined) return -1;
      if (bi === undefined) return 1;
      if (typeof ai === 'number' && typeof bi === 'number') { if (ai !== bi) return ai - bi; }
      else if (typeof ai === typeof bi) { if (ai < bi) return -1; if (ai > bi) return 1; }
      else return typeof ai === 'number' ? -1 : 1;
    }
    return 0;
  }


  function operationNeedsConfirmation(url) {
    if (!url) return false;
    return /\/api\/(?:integration\/[^/]+\/(?:update|update-alt|update-inplace)|driver\/[^/]+\/update|self-update(?:-inplace)?)(?:\?|$)/.test(url);
  }

  function selectedOperationVersion(elt, url) {
    try {
      const u = new URL(url, window.location.href);
      const fromUrl = u.searchParams.get('version');
      if (fromUrl) return fromUrl;
    } catch (_) {}
    const form = elt?.closest?.('form') || (elt?.tagName === 'FORM' ? elt : null);
    const inputVersion = form?.querySelector?.('input[name="version"]')?.value;
    return inputVersion || '';
  }

  function currentVersionForOperation(elt) {
    const targetId = elt?.getAttribute?.('hx-target');
    if (targetId && targetId.startsWith('#card-')) {
      const driverId = targetId.replace('#card-', '');
      const overlay = document.getElementById('upgrade-overlay-' + driverId);
      return overlay?.getAttribute('data-current-version') || '';
    }
    const card = elt?.closest?.('[id^="card-"]');
    if (card) {
      const driverId = card.id.replace('card-', '');
      const overlay = document.getElementById('upgrade-overlay-' + driverId);
      return overlay?.getAttribute('data-current-version') || '';
    }
    return '';
  }

  function operationConfirmationDetails(elt, url) {
    const targetVersion = selectedOperationVersion(elt, url);
    const currentVersion = currentVersionForOperation(elt);
    const isDowngrade = currentVersion && targetVersion && compareVersions(targetVersion, currentVersion) < 0;
    const isSelfUpdate = /\/api\/self-update/.test(url || '');

    if (isDowngrade) {
      return {
        title: 'Confirm Downgrade',
        tone: 'danger',
        icon: 'fa-triangle-exclamation',
        heading: `Downgrade from ${currentVersion} to ${targetVersion}?`,
        message: 'Downgrading can break functionality, invalidate migrations, or cause data/configuration loss. Continue only if you have a verified backup and know this version is compatible.',
        confirmLabel: 'Downgrade anyway',
        currentVersion,
        targetVersion
      };
    }

    if (targetVersion && currentVersion) {
      return {
        title: 'Confirm Version Change',
        tone: 'warning',
        icon: 'fa-arrows-rotate',
        heading: `Change version from ${currentVersion} to ${targetVersion}?`,
        message: 'This may restart the integration and can affect configuration, entity registration, or active automations.',
        confirmLabel: 'Change version',
        currentVersion,
        targetVersion
      };
    }

    if (targetVersion) {
      return {
        title: isSelfUpdate ? 'Confirm Integration Manager Update' : 'Confirm Version Change',
        tone: 'warning',
        icon: 'fa-arrows-rotate',
        heading: `Install ${targetVersion}?`,
        message: isSelfUpdate ? 'Integration Manager will update and restart. The UI may be temporarily unavailable.' : 'This may restart the integration and can affect configuration or entity registration.',
        confirmLabel: isSelfUpdate ? 'Update Integration Manager' : 'Install version',
        currentVersion: '',
        targetVersion
      };
    }

    return {
      title: 'Confirm Integration Update',
      tone: 'warning',
      icon: 'fa-arrows-rotate',
      heading: 'Update this integration?',
      message: 'This may restart the integration and can affect configuration or entity registration.',
      confirmLabel: 'Update integration',
      currentVersion: '',
      targetVersion: ''
    };
  }

  async function fetchOperationConfirmationModal(details) {
    const response = await fetch('/api/modal/operation-confirm', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'HX-Request': 'true'
      },
      credentials: 'same-origin',
      body: JSON.stringify(details)
    });

    if (!response.ok) {
      throw new Error(`Failed to load confirmation modal (${response.status})`);
    }

    return response.text();
  }

  document.body.addEventListener('htmx:confirm', function (event) {
    const elt = event.detail && event.detail.elt;
    const url = elt?.getAttribute?.('hx-post') || '';
    if (!operationNeedsConfirmation(url)) return;

    event.preventDefault();
    const details = operationConfirmationDetails(elt, url);

    openModal(details.title);

    fetchOperationConfirmationModal(details)
      .then(function (html) {
        const content = document.getElementById('modal-content');
        if (content) content.innerHTML = html;

        const titleSource = content?.querySelector?.('[data-modal-title]');
        if (titleSource?.dataset?.modalTitle) {
          updateModalTitle(titleSource.dataset.modalTitle);
        }

        const submit = document.getElementById('operation-confirm-submit');
        const cancel = document.getElementById('operation-confirm-cancel');

        cancel?.addEventListener('click', function () {
          closeModal();
        }, { once: true });

        submit?.addEventListener('click', function () {
          const driverId = (elt.getAttribute('hx-target') || '').replace('#card-', '');
          const overlay = driverId ? document.getElementById('upgrade-overlay-' + driverId) : null;
          const textElement = overlay?.querySelector('.upgrade-text');
          if (overlay) overlay.classList.remove('hidden');
          if (textElement && details.targetVersion && details.currentVersion) {
            textElement.textContent = details.tone === 'danger' ? 'Downgrading...' : 'Upgrading...';
          }
          closeModal();
          event.detail.issueRequest(true);
        }, { once: true });
      })
      .catch(function (error) {
        const content = document.getElementById('modal-content');
        if (content) {
          content.innerHTML = `
            <div class="space-y-4" data-modal-title="Confirmation Error">
              <div class="flex items-start gap-3 p-4 bg-red-50 dark:bg-red-500/10 border border-red-400 dark:border-red-500/30 rounded-lg">
                <i class="fa-solid fa-circle-exclamation text-red-600 dark:text-red-400 text-xl mt-0.5"></i>
                <div>
                  <h3 class="text-lg font-semibold text-gray-900 dark:text-white mb-1">Could not load confirmation</h3>
                  <p id="operation-confirm-error-message" class="text-sm text-gray-700 dark:text-gray-300"></p>
                </div>
              </div>
              <div class="flex justify-end pt-4 border-t border-gray-300 dark:border-uc-border">
                <button type="button" onclick="closeModal()" class="px-4 py-2 bg-gray-200 dark:bg-uc-card hover:bg-gray-300 dark:hover:bg-uc-darker text-gray-700 dark:text-gray-300 text-sm font-medium rounded-lg transition-colors border border-gray-300 dark:border-uc-border">Close</button>
              </div>
            </div>
          `;
          const message = document.getElementById('operation-confirm-error-message');
          if (message) message.textContent = String(error.message || error);
        }
        updateModalTitle('Confirmation Error');
      });
  });

  function initVersionSelector(root = document) {
    root.querySelectorAll?.('[hx-post]').forEach(button => {
      if (button.dataset.versionHandlerBound) return;
      button.dataset.versionHandlerBound = 'true';
      button.addEventListener('click', function () {
        const url = this.getAttribute('hx-post');
        const versionMatch = url && url.match(/[?&]version=([^&]+)/);
        const targetId = this.getAttribute('hx-target');
        if (!versionMatch || !targetId) return;
        const selectedVersion = decodeURIComponent(versionMatch[1]);
        const driverId = targetId.replace('#card-', '');
        const overlay = $('upgrade-overlay-' + driverId);
        if (!overlay) return;
        const currentVersion = overlay.getAttribute('data-current-version');
        const textElement = overlay.querySelector('.upgrade-text');
        if (currentVersion && textElement && selectedVersion) {
          textElement.textContent = compareVersions(selectedVersion, currentVersion) < 0 ? 'Downgrading...' : 'Upgrading...';
        }
      });
    });
  }

  function initPartialBehaviors(root = document) {
    initRemoteSelector(root);
    initVersionSelector(root);
  }

  window.initPartialBehaviors = initPartialBehaviors;

  document.addEventListener('DOMContentLoaded', function () {
    initPartialBehaviors(document);
    syncStoredRemote();
  });
  document.body.addEventListener('htmx:afterSwap', function (event) {
    initPartialBehaviors(event.target || document);
  });
})();
