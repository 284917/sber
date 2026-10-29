// Интерфейс кабинета: плавное появление блоков, счётчики, вкладки и выезжающие панели.
// Без JS всё видно и работает: классы появления ставит только этот скрипт.
(function () {
    const reduce = matchMedia('(prefers-reduced-motion: reduce)').matches;
    const ease = (t) => 1 - Math.pow(1 - t, 4);

    // --- Появление: блоки поднимаются на 32px и проявляются один раз, по очереди в пределах одного кадра
    const main = document.getElementById('main');
    if (main && 'IntersectionObserver' in window) {
        const items = [...main.querySelectorAll(':scope > *:not(script):not(dialog), [data-reveal] > *')]
            .filter((el) => !el.matches('.page-head, .back, [role=tabpanel], [data-reveal]'));
        const head = main.querySelector(':scope > .page-head');
        if (head) items.unshift(head);
        let batch = 0, frame = 0;
        const io = new IntersectionObserver((entries) => {
            entries.forEach((entry) => {
                if (!entry.isIntersecting) return;
                const el = entry.target;
                el.style.transitionDelay = Math.min(batch++ * 60, 400) + 'ms';
                el.classList.add('in');
                io.unobserve(el);
                el.addEventListener('transitionend', () => { el.style.transitionDelay = ''; el.classList.add('rv-done'); }, {once: true});
            });
            cancelAnimationFrame(frame);
            frame = requestAnimationFrame(() => { batch = 0; });
        }, {rootMargin: '0px 0px -40px 0px'});
        items.forEach((el) => { el.classList.add('rv'); io.observe(el); });
    }

    // --- Счётчики в карточках статистики: от нуля до значения
    document.querySelectorAll('[data-count], .stat .value').forEach((el) => {
        const text = el.textContent.trim();
        if (!/^\d{1,6}$/.test(text) || reduce) return;
        const target = +text, start = performance.now(), dur = 900;
        if (!target) return;
        el.textContent = '0';
        const tick = (now) => {
            const t = Math.min((now - start) / dur, 1);
            el.textContent = Math.round(target * ease(t));
            if (t < 1) requestAnimationFrame(tick);
        };
        requestAnimationFrame(tick);
    });

    // --- Вкладки внутри страницы: [data-tabs] > .seg-tabs > [role=tab][aria-controls]
    document.querySelectorAll('[data-tabs]').forEach((box) => {
        const tabs = [...box.querySelectorAll(':scope > .seg-tabs [role=tab]')];
        const ink = box.querySelector(':scope > .seg-tabs .seg-ink');
        const panels = tabs.map((t) => document.getElementById(t.getAttribute('aria-controls')));
        const moveInk = (tab) => {
            if (!ink || !tab.offsetWidth) return;
            ink.style.transform = 'translateX(' + tab.offsetLeft + 'px) scaleX(' + (tab.offsetWidth / 100) + ')';
        };
        const show = (i, focus, push) => {
            tabs.forEach((t, j) => {
                const on = i === j;
                t.setAttribute('aria-selected', on);
                t.tabIndex = on ? 0 : -1;
                panels[j].hidden = !on;
                panels[j].classList.toggle('tab-in', on);
            });
            moveInk(tabs[i]);
            if (focus) tabs[i].focus();
            if (push && box.dataset.tabs === 'hash') history.replaceState(null, '', '#' + panels[i].id);
        };
        tabs.forEach((t, i) => {
            t.addEventListener('click', () => show(i, false, true));
            t.addEventListener('keydown', (e) => {
                const d = e.key === 'ArrowRight' ? 1 : e.key === 'ArrowLeft' ? -1 : 0;
                if (d) { e.preventDefault(); show((i + d + tabs.length) % tabs.length, true, true); }
            });
        });
        // Открыть вкладку по якорю: #my-offers или любой элемент внутри панели
        const fromHash = () => {
            const target = location.hash && document.getElementById(decodeURIComponent(location.hash.slice(1)));
            const i = target ? panels.findIndex((p) => p.contains(target)) : -1;
            return i;
        };
        const preset = tabs.findIndex((t) => t.dataset.default !== undefined);
        const start = fromHash();
        show(start >= 0 ? start : Math.max(preset, 0), false, false);
        if (ink) requestAnimationFrame(() => ink.classList.add('ready'));
        addEventListener('hashchange', () => { const i = fromHash(); if (i >= 0) show(i, false, false); });
        addEventListener('resize', () => moveInk(tabs.find((t) => t.getAttribute('aria-selected') === 'true')));
        document.fonts && document.fonts.ready.then(() => moveInk(tabs.find((t) => t.getAttribute('aria-selected') === 'true')));
        // Ссылки на вкладку с этой же страницы
        document.addEventListener('click', (e) => {
            const a = e.target.closest('a[href^="#"], a[href^="/dashboard#"]');
            if (!a) return;
            const id = a.getAttribute('href').split('#')[1];
            const i = panels.findIndex((p) => p.id === id || p.querySelector('#' + CSS.escape(id)));
            if (i >= 0) { e.preventDefault(); show(i, false, true); box.scrollIntoView({behavior: reduce ? 'auto' : 'smooth', block: 'start'}); }
        });
    });

    // --- Выезжающая панель справа: <dialog class="drawer">, открывается кнопкой [data-open="id"]
    const closeDrawer = (dlg) => {
        if (!dlg.open || dlg.classList.contains('closing')) return;
        if (reduce) { dlg.close(); return; }
        dlg.classList.add('closing');
        dlg.addEventListener('animationend', () => { dlg.classList.remove('closing'); dlg.close(); }, {once: true});
    };
    document.addEventListener('click', (e) => {
        const opener = e.target.closest('[data-open]');
        if (opener) {
            const dlg = document.getElementById(opener.dataset.open);
            if (dlg && dlg.showModal) { e.preventDefault(); dlg.showModal(); const f = dlg.querySelector('input:not([type=hidden]), select, textarea'); f && f.focus(); }
            return;
        }
        const closer = e.target.closest('[data-close]');
        if (closer) { closeDrawer(closer.closest('dialog')); return; }
        if (e.target.matches && e.target.matches('dialog.drawer')) closeDrawer(e.target);
    });
    document.querySelectorAll('dialog.drawer').forEach((dlg) => {
        dlg.addEventListener('cancel', (e) => { e.preventDefault(); closeDrawer(dlg); });
        if (dlg.dataset.autoopen !== undefined && dlg.showModal) dlg.showModal();
    });

    // --- Плавное раскрытие <details>: содержимое проявляется, а при закрытии сворачивается
    const fold = (d) => {
        if (reduce) { d.open = false; return; }
        d.classList.add('closing');
        setTimeout(() => { d.open = false; d.classList.remove('closing'); }, 220);
    };
    document.querySelectorAll('details.smooth').forEach((d) => {
        d.querySelector(':scope > summary').addEventListener('click', (e) => {
            if (!d.open) return;
            e.preventDefault();
            fold(d);
        });
        // Открыли форму — сразу ставим курсор в первое пустое поле
        d.addEventListener('toggle', () => {
            if (!d.open) return;
            const f = d.querySelector('input:not([type=hidden]):not([value]), textarea, input[name=rate]');
            f && f.focus({preventScroll: true});
        });
    });
    document.addEventListener('click', (e) => {
        const btn = e.target.closest('[data-fold]');
        if (btn) fold(btn.closest('details'));
    });
})();
