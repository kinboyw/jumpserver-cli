// ==UserScript==
// @name         JumpServer Session Copy
// @namespace    local.jumpserver
// @version      0.2
// @description  Copy JumpServer cookies for jump-cli. Uses GM_cookie when available.
// @match        https://jumpserver.example.com/*
// @grant        GM_setClipboard
// @grant        GM_cookie
// ==/UserScript==

(function () {
  'use strict';

  function listGmCookies() {
    return new Promise((resolve) => {
      if (typeof GM_cookie === 'undefined' || !GM_cookie || typeof GM_cookie.list !== 'function') {
        resolve({ supported: false, cookies: [] });
        return;
      }

      GM_cookie.list({ url: location.origin + '/' }, (cookies, error) => {
        if (error) {
          resolve({ supported: true, error: String(error), cookies: [] });
          return;
        }
        resolve({ supported: true, cookies: cookies || [] });
      });
    });
  }

  async function copySession() {
    const gm = await listGmCookies();
    const payload = {
      origin: location.origin,
      href: location.href,
      cookie: document.cookie,
      gmCookieSupported: gm.supported,
      gmCookieError: gm.error || null,
      gmCookies: gm.cookies.map((cookie) => ({
        name: cookie.name,
        value: cookie.value,
        domain: cookie.domain,
        path: cookie.path,
        secure: cookie.secure,
        httpOnly: cookie.httpOnly,
        expirationDate: cookie.expirationDate || null
      })),
      copiedAt: new Date().toISOString()
    };

    const names = new Set(payload.gmCookies.map((cookie) => cookie.name));
    document.cookie.split(';').forEach((part) => {
      const trimmed = part.trim();
      if (!trimmed || !trimmed.includes('=')) return;
      const idx = trimmed.indexOf('=');
      names.add(trimmed.slice(0, idx));
    });

    GM_setClipboard(JSON.stringify(payload, null, 2), 'text');
    alert('JumpServer session copied. Cookies: ' + Array.from(names).sort().join(', '));
  }

  const btn = document.createElement('button');
  btn.textContent = 'Copy JMS Session';
  btn.style.cssText = [
    'position:fixed',
    'right:16px',
    'bottom:16px',
    'z-index:2147483647',
    'padding:8px 12px',
    'background:#1677ff',
    'color:#fff',
    'border:0',
    'border-radius:6px',
    'font-size:13px',
    'cursor:pointer',
    'box-shadow:0 2px 8px rgba(0,0,0,.2)'
  ].join(';');

  btn.addEventListener('click', copySession);
  document.documentElement.appendChild(btn);
})();
