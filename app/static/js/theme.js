(function(){
    var root = document.documentElement;
    function current(){ return root.getAttribute('data-theme') || 'dark'; }
    function sync(){
        var dark = current() === 'dark';
        document.querySelectorAll('.theme-ico').forEach(function(el){ el.textContent = dark ? '☀️' : '🌙'; });
        document.querySelectorAll('.theme-switch input').forEach(function(c){ c.checked = dark; });
    }
    window.setTheme = function(t){ root.setAttribute('data-theme', t); try{ localStorage.setItem('theme', t); }catch(e){} sync(); };
    window.toggleTheme = function(){ window.setTheme(current() === 'dark' ? 'light' : 'dark'); };
    document.addEventListener('DOMContentLoaded', sync);

    /* Копирование в буфер с визуальным подтверждением */
    window.tgCopy = function(text, btn, okLabel){
        function done(){
            if(!btn) return;
            if(!btn.getAttribute('data-label')) btn.setAttribute('data-label', btn.textContent);
            btn.textContent = okLabel || '✓ Скопировано'; btn.classList.add('copied');
            setTimeout(function(){ btn.textContent = btn.getAttribute('data-label'); btn.classList.remove('copied'); }, 1500);
        }
        if(navigator.clipboard && navigator.clipboard.writeText){
            navigator.clipboard.writeText(text).then(done).catch(function(){ fb(text); done(); });
        } else { fb(text); done(); }
    };
    function fb(text){ var ta=document.createElement('textarea'); ta.value=text; ta.style.position='fixed'; ta.style.opacity='0'; document.body.appendChild(ta); ta.select(); try{document.execCommand('copy');}catch(e){} document.body.removeChild(ta); }

    /* Эффект «волны» при нажатии (палец и мышь) */
    function ripple(e){
        var t = e.target.closest('.btn, .copy-btn, .nav-link');
        if(!t) return;
        var rect = t.getBoundingClientRect();
        var pt = (e.touches && e.touches[0]) ? e.touches[0] : e;
        var x = (pt.clientX != null ? pt.clientX - rect.left : rect.width/2);
        var y = (pt.clientY != null ? pt.clientY - rect.top : rect.height/2);
        var d = Math.max(rect.width, rect.height);
        var s = document.createElement('span');
        s.className = 'ink';
        s.style.width = s.style.height = d + 'px';
        s.style.left = (x - d/2) + 'px';
        s.style.top = (y - d/2) + 'px';
        t.appendChild(s);
        setTimeout(function(){ s.remove(); }, 560);
    }
    document.addEventListener('pointerdown', ripple, true);

    /* Жидкая подсветка-линза: свет следует за курсором по стеклянным поверхностям */
    var raf = 0, lastEv = null;
    function apply(){
        raf = 0;
        var e = lastEv; if(!e) return;
        var t = e.target.closest('.card, .panel, .auth-card');
        if(!t) return;
        var r = t.getBoundingClientRect();
        t.style.setProperty('--mx', (e.clientX - r.left) + 'px');
        t.style.setProperty('--my', (e.clientY - r.top) + 'px');
    }
    document.addEventListener('pointermove', function(e){
        if(e.pointerType === 'touch') return;   /* только для мыши */
        lastEv = e;
        if(!raf) raf = requestAnimationFrame(apply);
    }, { passive: true });
})();
