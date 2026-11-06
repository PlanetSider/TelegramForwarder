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
