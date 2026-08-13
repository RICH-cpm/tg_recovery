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

    /* Перевод одного ключа. Если строки нет — возвращаем null, и вызывающий
       оставляет текст, отрисованный сервером. Показывать вместо подписи сам
       ключ («user_accounts») хуже, чем показать её на другом языке. */
    function lookup(key) {
        var dict = (window.TG_STRINGS || {})[lang()] || {};
        return Object.prototype.hasOwnProperty.call(dict, key) ? dict[key] : null;
    }

    window.tgT = function (key) {
        var v = lookup(key);
        return v === null ? key : v;
    };

    function applyLang() {
        var L = lang();
        root.setAttribute('lang', L);
        document.querySelectorAll('[data-i18n]').forEach(function (el) {
            var v = lookup(el.getAttribute('data-i18n'));
            if (v) el.textContent = v;
        });
        document.querySelectorAll('[data-i18n-placeholder]').forEach(function (el) {
            var v = lookup(el.getAttribute('data-i18n-placeholder'));
            if (v) el.placeholder = v;
        });
        document.querySelectorAll('[data-i18n-title]').forEach(function (el) {
            var v = lookup(el.getAttribute('data-i18n-title'));
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
        if (box) {
            box.closest('[data-form-switch]').classList.toggle('on', box.checked);
            return;
        }
        var file = e.target.closest('.file-pick input[type=file]');
        if (file) {
            var wrap = file.closest('.file-pick');
            var label = wrap.querySelector('.file-pick-name');
            var picked = file.files && file.files.length ? file.files[0].name : '';
            wrap.classList.toggle('has-file', !!picked);
            if (picked) {
                label.textContent = picked;
                label.removeAttribute('data-i18n');
            } else {
                label.setAttribute('data-i18n', 'no_file');
                label.textContent = window.tgT('no_file');
            }
        }
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

    /* ------------------------------------------------------- анимации */
    var reduced = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

    /* Волна от точки нажатия — подтверждает, что элемент отреагировал. */
    function ripple(e) {
        if (reduced) return;
        var t = e.target.closest('.btn, .chip-btn, .copy-btn, .nav-link, .icon-btn');
        if (!t || t.dataset.noInk) return;
        var r = t.getBoundingClientRect();
        var d = Math.max(r.width, r.height);
        var s = document.createElement('span');
        s.className = 'ink';
        s.style.width = s.style.height = d + 'px';
        s.style.left = ((e.clientX != null ? e.clientX - r.left : r.width / 2) - d / 2) + 'px';
        s.style.top = ((e.clientY != null ? e.clientY - r.top : r.height / 2) - d / 2) + 'px';
        t.appendChild(s);
        setTimeout(function () { s.remove(); }, 560);
    }
    document.addEventListener('pointerdown', ripple, true);

    /* Числа в плитках набегают, а не появляются готовыми. */
    function countUp(el) {
        var target = parseInt(el.textContent.trim(), 10);
        if (isNaN(target) || target === 0 || reduced) return;
        var start = performance.now(), dur = Math.min(900, 260 + target * 40);
        el.style.minWidth = el.getBoundingClientRect().width + 'px';
        (function step(now) {
            var p = Math.min(1, (now - start) / dur);
            var eased = 1 - Math.pow(1 - p, 3);
            el.textContent = Math.round(target * eased);
            if (p < 1) requestAnimationFrame(step);
            else el.textContent = target;
        })(start);
    }

    /* Сообщения об успехе исчезают сами, ошибки остаются до прочтения. */
    function autoDismiss() {
        document.querySelectorAll('.alert-ok[data-toast]').forEach(function (el) {
            setTimeout(function () {
                el.classList.add('leaving');
                setTimeout(function () { el.remove(); }, 400);
            }, 5000);
        });
    }

    function init() {
        syncTheme();
        applyLang();
        document.querySelectorAll('.stat-num').forEach(countUp);
        autoDismiss();
    }
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
    else init();
})();
