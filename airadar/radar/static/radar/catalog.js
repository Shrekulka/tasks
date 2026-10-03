(() => {
    const form = document.getElementById("filters");              // есть только в каталоге
    const box = document.getElementById("results") || document;   // на главной слушаем весь документ
    const bar = document.getElementById("cmp-bar");

    if (!bar) return;

    const MAX_COMPARE = Number(bar.dataset.max) || 3;   // ліміт приходить з settings.MAX_COMPARE через data-max

    // ---------- вибір для порівняння: живе у sessionStorage, тому переживає переходи між сторінками ----------
    const STORAGE_KEY = "airadar:compare";

    const loadPicked = () => {
        try {
            const v = JSON.parse(sessionStorage.getItem(STORAGE_KEY));
            return Array.isArray(v)
                ? v.filter((d) => typeof d === "string" && d).slice(0, MAX_COMPARE)
                : [];
        } catch (error) {
            return [];
        }
    };

    const savePicked = () => {
        try {
            sessionStorage.setItem(STORAGE_KEY, JSON.stringify([...picked]));
        } catch (error) { /* сховище недоступне: працюємо без збереження */ }
    };

    const picked = new Set(loadPicked());

    // домен зі сторінки сайту (?compare=домен) додається до вже обраних, якщо не перевищено ліміт
    const initialCompare = document.getElementById("initial-compare-data");

    if (initialCompare) {
        try {
            const value = JSON.parse(initialCompare.textContent);

            if (typeof value === "string" && value.trim()) {
                const domain = value.trim();
                if (picked.has(domain) || picked.size < MAX_COMPARE) picked.add(domain);
            }
        } catch (error) {
            console.error("AI Radar: invalid initial compare data", error);
        }
    }

    // ?compare=домен потрібен лише один раз: без прибирання з URL «Скинути» не спрацює після перезавантаження
    const stripCompareParam = () => {
        const url = new URL(window.location.href);
        if (!url.searchParams.has("compare")) return;
        url.searchParams.delete("compare");
        history.replaceState(null, "", url.pathname + url.search + url.hash);
    };

    let timer = null;
    let controller = null;

    const currentQuery = () => {
        const params = new URLSearchParams();
        for (const [k, v] of new FormData(form)) if (v !== "") params.set(k, v);
        return params.toString();
    };

    const syncCompare = () => {
        savePicked();

        box.querySelectorAll(".cmp").forEach((cb) => {
            cb.checked = picked.has(cb.value);
        });

        const n = picked.size;

        bar.classList.toggle("d-none", n === 0);

        const count = document.getElementById("cmp-count");
        const link = document.getElementById("cmp-link");

        count.textContent = `Обрано: ${n} з ${MAX_COMPARE}`;

        // підказка потрібна лише поки обрано менше двох сайтів
        const hint = bar.querySelector(".cmp-bar__hint");
        if (hint) hint.classList.toggle("d-none", n >= 2);

        const qs = [...picked]
            .map((domain) => `d=${encodeURIComponent(domain)}`)
            .join("&");

        link.href = `${bar.dataset.compareUrl}?${qs}`;

        const disabled = n < 2;

        link.classList.toggle("disabled", disabled);
        link.setAttribute("aria-disabled", String(disabled));

        if (disabled) {
            link.setAttribute("tabindex", "-1");
        } else {
            link.removeAttribute("tabindex");
        }
    };

    // ---------- каталог: живий пошук і «Показати ще» (на головній не потрібні) ----------
    async function load(url, append = false) {
        // AbortController не даёт устаревшему ответу перезаписать более новый
        if (controller) controller.abort();
        const mine = controller = new AbortController();
        box.setAttribute("aria-busy", "true");
        try {
            const res = await fetch(url + (append ? (url.includes("?") ? "&" : "?") + "append=1" : ""), {
                headers: {"X-Requested-With": "XMLHttpRequest"},
                signal: mine.signal,
            });
            const html = await res.text();
            if (append && !res.ok) {
                // збій «Показати ще» не повинен стирати вже показаний список
                const more = box.querySelector("[data-more]");
                if (more) more.insertAdjacentHTML("afterbegin", '<div class="text-danger small mb-2" role="alert">Не вдалося завантажити. Спробуйте ще раз.</div>');
            } else if (append) {
                box.querySelector("[data-more]")?.remove();
                box.querySelector("#cards").insertAdjacentHTML("beforeend", html);
            } else {
                box.innerHTML = html;
            }
            syncCompare();
        } catch (e) {
            if (e.name === "AbortError") return;
            box.innerHTML = '<div class="alert alert-danger">Помилка мережі. <button class="btn btn-sm btn-outline-danger" data-retry>Повторити</button></div>';
        } finally {
            if (controller === mine) box.removeAttribute("aria-busy");
        }
    }

    const reload = () => {
        const qs = currentQuery();
        const url = form.getAttribute("action") + (qs ? "?" + qs : "");
        history.replaceState(null, "", url);
        load(url);
    };

    if (form) {   // форма і «Показати ще» є лише в каталозі
        form.addEventListener("input", () => {
            clearTimeout(timer);
            timer = setTimeout(reload, 400);
        });  // debounce

        form.addEventListener("submit", e => {
            e.preventDefault();
            clearTimeout(timer);
            reload();
        });

        box.addEventListener("click", e => {
            const more = e.target.closest("[data-more] a");
            if (more) {
                e.preventDefault();
                load(more.getAttribute("href"), true);
            }
            if (e.target.closest("[data-retry]")) {
                e.preventDefault();
                reload();
            }
        });
    }

    // ---------- чекбокси «Порівняти» (працюють і на головній, і в каталозі) ----------
    box.addEventListener("change", e => {
        const cb = e.target.closest(".cmp");
        if (!cb) return;
        if (cb.checked && picked.size >= MAX_COMPARE) {
            cb.checked = false;
            return;
        }
        cb.checked ? picked.add(cb.value) : picked.delete(cb.value);
        syncCompare();
    });

    // «Скинути»: прибрати весь вибір і сховати панель
    document.getElementById("cmp-clear")?.addEventListener("click", () => {
        picked.clear();
        stripCompareParam();
        syncCompare();
    });

    // «Назад»/«Вперед»: браузер відновлює стан чекбоксів уже після виконання скриптів (або віддає сторінку з bfcache),
    // тому джерелом істини є sessionStorage, і галочки перезаписуються на кожному pageshow
    window.addEventListener("pageshow", () => {
        picked.clear();
        loadPicked().forEach((d) => picked.add(d));
        syncCompare();
    });

    stripCompareParam();
    syncCompare();   // показати панель одразу: вибір зберігся з іншої сторінки або прийшов через ?compare=домен
})();