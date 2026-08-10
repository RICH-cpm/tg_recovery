/* Тема, язык интерфейса, копирование, подсветка стекла. */
(function () {
    var root = document.documentElement;

    /* ------------------------------------------------------------- тема */
    function theme() { return root.getAttribute('data-theme') === 'light' ? 'light' : 'dark'; }

    function syncTheme() {
        var dark = theme() === 'dark';
        document.querySelectorAll('.ico-dark').forEach(function (el) { el.hidden = !dark; });
        document.querySelectorAll('.ico-light').forEach(function (el) { el.hidden = dark; });
        document.querySelectorAll('[data-theme-switch]').forEach(function (el) { el.classList.toggle('on', dark); });
    }

    window.setTheme = function (t) {
        root.setAttribute('data-theme', t === 'light' ? 'light' : 'dark');
        try { localStorage.setItem('theme', t); } catch (e) {}
        syncTheme();
    };
    window.toggleTheme = function () { window.setTheme(theme() === 'dark' ? 'light' : 'dark'); };

    /* ------------------------------------------------------------ язык */
    function lang() { return root.getAttribute('data-lang') === 'en' ? 'en' : 'ru'; }

    /* Перевод одного ключа — доступен и разметке страниц. */
    window.tgT = function (key) {
        var dict = (window.TG_STRINGS || {})[lang()] || {};
        return dict[key] !== undefined ? dict[key] : key;
    };

    function applyLang() {
        var L = lang();
        root.setAttribute('lang', L);
        document.querySelectorAll('[data-i18n]').forEach(function (el) {
            var v = window.tgT(el.getAttribute('data-i18n'));
            if (v) el.textContent = v;
        });
        document.querySelectorAll('[data-i18n-placeholder]').forEach(function (el) {
            var v = window.tgT(el.getAttribute('data-i18n-placeholder'));
            if (v) el.placeholder = v;
        });
        document.querySelectorAll('[data-i18n-title]').forEach(function (el) {
            var v = window.tgT(el.getAttribute('data-i18n-title'));
            if (v) { el.title = v; el.setAttribute('aria-label', v); }
        });
        document.querySelectorAll('[data-lang-set]').forEach(function (b) {
            b.classList.toggle('on', b.getAttribute('data-lang-set') === L);
        });
        if (window.tgRefreshTimes) window.tgRefreshTimes();
    }

    window.setLang = function (l) {
        root.setAttribute('data-lang', l === 'en' ? 'en' : 'ru');
        try { localStorage.setItem('lang', l); } catch (e) {}
        applyLang();
    };
    window.tgLang = lang;

    /* -------------------------------------------------------- копирование */
    function flash(btn, label) {
        if (!btn) return;
        if (!btn.dataset.origI18n) btn.dataset.origI18n = btn.getAttribute('data-i18n') || '';
        var back = btn.dataset.origI18n ? window.tgT(btn.dataset.origI18n) : btn.textContent;
        btn.textContent = label;
        btn.classList.add('copied');
        clearTimeout(btn._t);
        btn._t = setTimeout(function () { btn.textContent = back; btn.classList.remove('copied'); }, 1500);
    }

    function fallbackCopy(text) {
        var ta = document.createElement('textarea');
        ta.value = text;
        ta.style.position = 'fixed';
        ta.style.opacity = '0';
        document.body.appendChild(ta);
        ta.select();
        try { document.execCommand('copy'); } catch (e) {}
        document.body.removeChild(ta);
    }

    window.tgCopy = function (text, btn) {
        var done = function () { flash(btn, window.tgT('copied')); };
        if (navigator.clipboard && navigator.clipboard.writeText) {
            navigator.clipboard.writeText(text).then(done).catch(function () { fallbackCopy(text); done(); });
        } else {
            fallbackCopy(text);
            done();
        }
    };

    /* ---------------------------------------------------------- события */
    document.addEventListener('click', function (e) {
        var t = e.target.closest('[data-theme-toggle], [data-theme-switch], [data-lang-set], [data-copy]');
        if (!t) return;
        if (t.hasAttribute('data-copy')) { window.tgCopy(t.getAttribute('data-copy'), t); return; }
        if (t.hasAttribute('data-lang-set')) { window.setLang(t.getAttribute('data-lang-set')); return; }
        window.toggleTheme();
    });

    /* Переключатель-галочка в форме бэкапа: подсвечиваем по состоянию input. */
    document.addEventListener('change', function (e) {
        var box = e.target.closest('[data-form-switch] input');
        if (!box) return;
        box.closest('[data-form-switch]').classList.toggle('on', box.checked);
    });

    /* Подсветка-линза следует за курсором по стеклянным поверхностям. */
    var raf = 0, last = null;
    function moveGlass() {
        raf = 0;
        var e = last;
        if (!e) return;
        var t = e.target.closest && e.target.closest('.card, .panel, .auth-card, .glass');
        if (!t) return;
        var r = t.getBoundingClientRect();
        t.style.setProperty('--mx', (e.clientX - r.left) + 'px');
        t.style.setProperty('--my', (e.clientY - r.top) + 'px');
    }
    document.addEventListener('pointermove', function (e) {
        if (e.pointerType === 'touch') return;
        last = e;
        if (!raf) raf = requestAnimationFrame(moveGlass);
    }, { passive: true });

    function init() { syncTheme(); applyLang(); }
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
    else init();
})();
