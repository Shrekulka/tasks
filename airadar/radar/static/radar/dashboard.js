// Дашборд головної: три графіки з одного конструктора. Дані приходять через json_script,
// посилання на каталог через data-catalog-url (жодних захардкоджених URL чи категорій).
document.addEventListener("DOMContentLoaded", () => {
    if (typeof window.Chart !== "function") {
        console.error("AI Radar: Chart.js не завантажено.");
        return;
    }

    const readJson = (id) => {
        const el = document.getElementById(id);
        if (!el) return [];
        try {
            const value = JSON.parse(el.textContent);
            return Array.isArray(value) ? value : [];
        } catch (error) {
            console.error(`AI Radar: невалідний JSON у #${id}`, error);
            return [];
        }
    };

    const css = getComputedStyle(document.documentElement);
    const color = (name, fallback) => css.getPropertyValue(name).trim() || fallback;
    const C = {
        blue: color("--blue", "#3b82f6"),
        cyan: color("--cyan", "#22d3ee"),
        purple: color("--purple", "#a855f7"),
        text: color("--text", "#f4f7fb"),
        muted: color("--muted", "#7f8b9b"),
    };

    const rgba = (hex, a) => {
        const v = hex.replace("#", "");
        if (v.length !== 6) return `rgba(59,130,246,${a})`;
        const n = (i) => parseInt(v.slice(i, i + 2), 16);
        return `rgba(${n(0)},${n(2)},${n(4)},${a})`;
    };
    const fmt = (v) => (Number.isFinite(Number(v)) ? Number(v).toLocaleString("uk-UA") : "—");
    const compact = (v) =>
        Number.isFinite(Number(v))
            ? new Intl.NumberFormat("uk-UA", {notation: "compact", maximumFractionDigits: 1}).format(Number(v))
            : "—";

    const tooltip = {
        backgroundColor: "rgba(7,11,18,.96)",
        borderColor: rgba(C.blue, 0.35),
        borderWidth: 1,
        titleColor: C.text,
        bodyColor: C.text,
        padding: 12,
        displayColors: false,
    };
    const grid = {color: rgba(C.muted, 0.12)};
    const ticks = {color: C.muted};

    // Спільний конструктор. opts: {type, labels, values, label, horizontal, log, onPick}
    function makeChart(canvasId, opts) {
        const canvas = document.getElementById(canvasId);
        if (!canvas || !opts.values.length) return;
        const isLine = opts.type === "line";
        const valueAxis = {
            type: opts.log ? "logarithmic" : "linear",
            beginAtZero: !opts.log,
            min: opts.log ? 1 : undefined,
            grid,
            // на логарифмічній шкалі підписуємо лише степені десятки (1, 10, 100…), інакше з'являються «7 тис.», «3 тис.»
            ticks: {
                ...ticks,
                maxRotation: 0,
                callback: (v) => (opts.log && Math.abs(Math.log10(v) % 1) > 1e-9 ? "" : compact(v)),
            },
        };
        const labelAxis = {grid: {display: false}, ticks: {...ticks, maxTicksLimit: isLine ? 8 : undefined}};

        // точні значення над стовпчиками: на логарифмічній шкалі довжина не пропорційна кількості
        const valueLabels = {
            id: "valueLabels",
            afterDatasetsDraw(c) {
                if (isLine || !opts.valueLabels) return;
                const {ctx} = c;
                ctx.save();
                ctx.fillStyle = C.text;
                ctx.font = "11px system-ui, sans-serif";
                ctx.textAlign = opts.horizontal ? "left" : "center";
                ctx.textBaseline = opts.horizontal ? "middle" : "bottom";
                c.getDatasetMeta(0).data.forEach((bar, i) => {
                    const text = compact(opts.values[i]);
                    if (opts.horizontal) ctx.fillText(text, bar.x + 6, bar.y);
                    else ctx.fillText(text, bar.x, bar.y - 4);
                });
                ctx.restore();
            },
        };

        const chart = new Chart(canvas, {
            plugins: [valueLabels],
            type: opts.type,
            data: {
                labels: opts.labels,
                datasets: [{
                    label: opts.label,
                    data: opts.values,
                    borderColor: C.cyan,
                    backgroundColor: isLine ? rgba(C.cyan, 0.12) : rgba(C.blue, 0.75),
                    hoverBackgroundColor: C.cyan,
                    borderWidth: isLine ? 2 : 0,
                    borderRadius: isLine ? 0 : 6,
                    fill: isLine,
                    tension: 0,
                    segment: opts.partialLast ? {borderDash: (c) => (c.p1DataIndex === opts.values.length - 1 ? [6, 4] : undefined)} : undefined,
                    pointRadius: 0,
                    pointHoverRadius: 4,
                }],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                layout: {padding: opts.valueLabels ? {top: 18, right: 44} : 0},
                indexAxis: opts.horizontal ? "y" : "x",
                interaction: {mode: isLine ? "index" : "nearest", intersect: false},
                plugins: {
                    legend: {display: false},
                    tooltip: {...tooltip, callbacks: {label: (ctx) => `${fmt(ctx.parsed[opts.horizontal ? "x" : "y"])}`}},
                },
                scales: opts.horizontal
                    ? {x: valueAxis, y: labelAxis}
                    : {x: labelAxis, y: valueAxis},
                onClick: opts.onPick
                    ? (_event, elements) => {
                        if (elements.length) opts.onPick(elements[0].index);
                    }
                    : undefined,
                onHover: opts.onPick
                    ? (event, elements) => {
                        event.native.target.style.cursor = elements.length ? "pointer" : "default";
                    }
                    : undefined,
            },
        });
        return chart;
    }

    const goCatalog = (canvasId, params) => {
        const base = document.getElementById(canvasId)?.dataset.catalogUrl;
        if (base) window.location.href = `${base}?${new URLSearchParams(params)}`;
    };

    // 1) Нові сайти за день: ВЕСЬ індекс, усі ніші
    const daily = readJson("daily-stats-data");
    makeChart("dailySitesChart", {
        type: "line", label: "Нові сайти",
        labels: daily.map((d) => d[0]), values: daily.map((d) => Number(d[1]) || 0), partialLast: true,
    });

    // 2) Ніші: клік веде в каталог з фільтром niche=<назва>
    const niches = readJson("category-stats-data");
    makeChart("categoriesChart", {
        type: "bar", horizontal: true, valueLabels: true, label: "Сайтів",
        labels: niches.map((n) => n[0]), values: niches.map((n) => Number(n[1]) || 0),
        onPick: (i) => goCatalog("categoriesChart", {niche: niches[i][0]}),
    });

    // 3) DR по AI-сайтах: [початок бакета, кількість]; клік веде в каталог з dr_min=<початок>
    const dr = readJson("dr-stats-data");
    const drLabels = dr.map((b, i) => (dr[i + 1] ? `${b[0]}–${dr[i + 1][0] - 1}` : `${b[0]}+`));
    makeChart("drChart", {
        type: "bar", log: true, valueLabels: true, label: "Сайтів",
        labels: drLabels, values: dr.map((b) => Number(b[1]) || 0),
        onPick: (i) => goCatalog("drChart", dr[i + 1]
            ? {dr_min: dr[i][0], dr_max: dr[i + 1][0] - 1}
            : {dr_min: dr[i][0]}),
    });
});
