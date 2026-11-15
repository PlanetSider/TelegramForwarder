/* Web 控制台的少量交互：标签页切换、toast、请求封装。
   刻意不引入框架 —— 与现有 RSS 面板保持一致，无需构建步骤。 */
(function () {
  'use strict';

  /* ------------------------------------------------------------ 标签页 */
  var tabs = document.querySelectorAll('.tabs button[data-tab]');
  if (tabs.length) {
    tabs.forEach(function (btn) {
      btn.addEventListener('click', function () {
        var name = btn.dataset.tab;
        tabs.forEach(function (b) { b.classList.toggle('on', b === btn); });
        document.querySelectorAll('.tabpane').forEach(function (pane) {
          pane.classList.toggle('on', pane.id === 'tab-' + name);
        });
        try {
          history.replaceState(null, '', '#' + name);
        } catch (e) { /* file:// 下可能受限，忽略 */ }
      });
    });

    var hash = (location.hash || '').replace('#', '');
    if (hash) {
      var target = document.querySelector('.tabs button[data-tab="' + hash + '"]');
      if (target) target.click();
    }
  }

  /* ------------------------------------------------------------ toast */
  var box = document.getElementById('toast');
  window.toast = function (message, type) {
    if (!box) { console.log('[toast]', message); return; }
    var el = document.createElement('div');
    el.className = 'toast-item ' + (type || 'info');
    el.textContent = message;
    box.appendChild(el);
    setTimeout(function () {
      el.classList.add('out');
      setTimeout(function () { el.remove(); }, 250);
    }, 2800);
  };

  /* ------------------------------------------------------------ 请求封装 */
  window.api = function (method, url, body) {
    var opts = {
      method: method,
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' }
    };
    if (body !== undefined) opts.body = JSON.stringify(body);
    return fetch(url, opts).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (data) {
        if (!r.ok) {
          var msg = (data && (data.error || data.detail)) || ('HTTP ' + r.status);
          throw new Error(msg);
        }
        return data;
      });
    });
  };

  /* ------------------------------------------------------------ 表单收集
     把 [data-field] 控件收集成 { "a.b": value } 形式的对象，
     供 P1 的保存接口直接消费（点号表示嵌套字段）。 */
  window.collectFields = function (root) {
    var out = {};
    (root || document).querySelectorAll('[data-field]').forEach(function (el) {
      var key = el.dataset.field;
      if (!key) return;
      if (el.type === 'checkbox') {
        out[key] = !!el.checked;
      } else if (el.type === 'number') {
        out[key] = el.value === '' ? null : Number(el.value);
      } else {
        out[key] = el.value;
      }
    });
    return out;
  };
})();

/* ------------------------------------------------------------ 移动端导航抽屉 */
(function () {
  var btn = document.getElementById('navToggle');
  var side = document.getElementById('sidebar');
  var scrim = document.getElementById('scrim');
  if (!btn || !side || !scrim) return;

  function setOpen(open) {
    document.body.classList.toggle('menu-open', open);
    btn.setAttribute('aria-expanded', open ? 'true' : 'false');
  }

  btn.addEventListener('click', function () {
    setOpen(!document.body.classList.contains('menu-open'));
  });
  scrim.addEventListener('click', function () { setOpen(false); });
  side.addEventListener('click', function (e) {
    if (e.target.closest('a')) setOpen(false);
  });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && document.body.classList.contains('menu-open')) setOpen(false);
  });
  var mq = window.matchMedia('(max-width: 768px)');
  var onChange = function () { if (!mq.matches) setOpen(false); };
  if (mq.addEventListener) mq.addEventListener('change', onChange);
  else if (mq.addListener) mq.addListener(onChange);
})();

/* 重启服务：确认后调接口，等服务回来再自动刷新页面 */
function restartService() {
  if (!confirm('确定要重启服务吗？正在处理的消息会中断，约 10 秒后恢复。')) return;
  fetch('/admin/api/restart', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' })
    .then(function (r) { return r.json().catch(function () { return {}; }); })
    .then(function (data) {
      if (data && data.ok === false) {
        alert('重启请求失败：' + (data.error || '未知原因'));
        return;
      }
      var tip = document.createElement('div');
      tip.style.cssText = 'position:fixed;left:50%;top:24px;transform:translateX(-50%);z-index:9999;background:#111;color:#fff;padding:10px 16px;border-radius:8px;font-size:13px';
      tip.textContent = '服务正在重启，恢复后会自动刷新…';
      document.body.appendChild(tip);
      waitForRestart(0);
    })
    .catch(function () { alert('重启请求发送失败，请检查网络'); });
}

function waitForRestart(attempt) {
  if (attempt > 60) { location.reload(); return; }
  setTimeout(function () {
    fetch('/admin/login', { method: 'GET', cache: 'no-store' })
      .then(function (r) { if (r.ok) location.reload(); else waitForRestart(attempt + 1); })
      .catch(function () { waitForRestart(attempt + 1); });
  }, 1000);
}
