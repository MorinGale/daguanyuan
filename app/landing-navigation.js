'use strict';
// Preserve the captured landing UI; local learning uses its own page renderer.
document.addEventListener('click', event => {
  const button = event.target.closest('button');
  if (button && (button.textContent.trim() === '开始学习' ||
      (button.textContent.trim() === '进入' && button.closest('[role="tabpanel"]')?.textContent.includes('考研数学')))) {
    event.preventDefault();
    event.stopImmediatePropagation();
    location.assign('/math');
    return;
  }
  const link = event.target.closest('a[href]');
  if (!link || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
  const url = new URL(link.href, location.href);
  if (url.origin === location.origin && (url.pathname.startsWith('/math') || url.pathname === '/settings')) {
    event.preventDefault();
    event.stopImmediatePropagation();
    location.assign(url.href);
  }
}, true);
