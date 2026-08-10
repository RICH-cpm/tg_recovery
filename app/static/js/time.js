/* Относительное время. Сервер отдаёт наивный ISO в UTC — добавляем Z,
   иначе браузер разберёт метку как локальное время и покажет сдвиг. */
(function () {
    function parse(iso) {
        return new Date(iso + (/[Zz]|[+-]\d\d:?\d\d$/.test(iso) ? '' : 'Z'));
    }

    function plural(n, one, few, many) {
        var m10 = n % 10, m100 = n % 100;
        if (m100 >= 11 && m100 <= 14) return many;
        if (m10 === 1) return one;
        if (m10 >= 2 && m10 <= 4) return few;
        return many;
    }

    function ru(s, m, h, d) {
        if (s < 10) return 'только что';
        if (s < 60) return s + ' сек назад';
        if (m < 60) return m + ' ' + plural(m, 'минута', 'минуты', 'минут') + ' назад';
        if (h < 24) return h + ' ' + plural(h, 'час', 'часа', 'часов') + ' назад';
        if (d < 7) return d + ' ' + plural(d, 'день', 'дня', 'дней') + ' назад';
        return null;
    }

    function en(s, m, h, d) {
        if (s < 10) return 'just now';
        if (s < 60) return s + ' sec ago';
        if (m < 60) return m + ' min ago';
        if (h < 24) return h + (h === 1 ? ' hour ago' : ' hours ago');
        if (d < 7) return d + (d === 1 ? ' day ago' : ' days ago');
        return null;
    }

    function format(iso, lang) {
        var d = parse(iso), now = new Date();
        if (isNaN(d)) return iso;
        var s = Math.max(0, Math.floor((now - d) / 1000));
        var m = Math.floor(s / 60), h = Math.floor(m / 60), day = Math.floor(h / 24);
        var rel = (lang === 'en' ? en : ru)(s, m, h, day);
        if (rel) return rel;
        return d.toLocaleDateString(lang === 'en' ? 'en-GB' : 'ru-RU', {
            day: 'numeric',
            month: 'short',
            year: d.getFullYear() !== now.getFullYear() ? 'numeric' : undefined,
            hour: '2-digit',
            minute: '2-digit'
        });
    }

    function refresh() {
        var lang = window.tgLang ? window.tgLang() : 'ru';
        var locale = lang === 'en' ? 'en-GB' : 'ru-RU';
        document.querySelectorAll('.time-ago').forEach(function (el) {
            var t = el.dataset.time;
            if (!t) return;
            el.textContent = format(t, lang);
            var abs = parse(t);
            if (!isNaN(abs)) el.title = abs.toLocaleString(locale);
        });
    }

    window.tgRefreshTimes = refresh;
    refresh();
    setInterval(refresh, 30000);
})();
