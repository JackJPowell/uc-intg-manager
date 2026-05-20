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
  document.body.addEventListener('htmx:afterSwap', function (event) {
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
    const button = root.querySelector?.('#remote-selector-button') || $('remote-selector-button');
    const menu = root.querySelector?.('#remote-selector-menu') || $('remote-selector-menu');
    if (!button || !menu || button.dataset.bound) return;
    button.dataset.bound = 'true';

    button.addEventListener('click', function (event) {
      event.stopPropagation();
      menu.classList.toggle('hidden');
    });

    root.querySelectorAll?.('.remote-selector-item').forEach(item => {
      if (item.dataset.bound) return;
      item.dataset.bound = 'true';
      item.addEventListener('click', function () {
        const remoteId = this.dataset.remoteId;
        fetch('/api/active-remote', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ remote_id: remoteId })
        })
          .then(response => response.json())
          .then(data => {
            if (data.status === 'ok') {
              localStorage.setItem('active_remote_id', remoteId);
              window.location.reload();
            } else {
              console.error('Failed to switch remote:', data.error);
              alert('Failed to switch remote: ' + data.error);
            }
          })
          .catch(error => {
            console.error('Error switching remote:', error);
            alert('Error switching remote: ' + error);
          });
      });
    });
  }

  document.addEventListener('click', function (event) {
    const button = $('remote-selector-button');
    const menu = $('remote-selector-menu');
    if (button && menu && !button.contains(event.target) && !menu.contains(event.target)) {
      menu.classList.add('hidden');
    }
  });

  function syncStoredRemote() {
    const storedRemoteId = localStorage.getItem('active_remote_id');
    if (!storedRemoteId || window.__remoteSyncDone) return;
    window.__remoteSyncDone = true;
    fetch('/api/active-remote')
      .then(response => response.json())
      .then(data => {
        if (data.id !== storedRemoteId) {
          return fetch('/api/active-remote', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ remote_id: storedRemoteId })
          }).then(res => { if (res.ok) window.location.reload(); });
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

  document.addEventListener('DOMContentLoaded', function () {
    initPartialBehaviors(document);
    syncStoredRemote();
  });
  document.body.addEventListener('htmx:afterSwap', function (event) {
    initPartialBehaviors(event.target || document);
  });
})();
