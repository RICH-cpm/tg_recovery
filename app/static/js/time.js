(function(){
    function fmt(iso){
        var d=new Date(iso+(iso.endsWith('Z')?'':'Z')),n=new Date();
        var s=Math.floor((n-d)/1000),m=Math.floor(s/60),h=Math.floor(m/60),day=Math.floor(h/24);
        if(s<10)return'только что';
        if(s<60)return s+' сек назад';
        if(m<60)return m+' '+pl(m,'минута','минуты','минут')+' назад';
        if(h<24)return h+' '+pl(h,'час','часа','часов')+' назад';
        if(day<7)return day+' '+pl(day,'день','дня','дней')+' назад';
        return d.toLocaleDateString('ru-RU',{day:'numeric',month:'short',year:d.getFullYear()!==n.getFullYear()?'numeric':undefined,hour:'2-digit',minute:'2-digit'});
    }
    function pl(n,a,b,c){var m10=n%10,m100=n%100;if(m100>=11&&m100<=14)return c;if(m10===1)return a;if(m10>=2&&m10<=4)return b;return c;}
    function upd(){document.querySelectorAll('.time-ago').forEach(function(el){var t=el.dataset.time;if(t){el.textContent=fmt(t);el.title=new Date(t+(t.endsWith('Z')?'':'Z')).toLocaleString('ru-RU');}});}
    upd();setInterval(upd,30000);
})();
