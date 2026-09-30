/* ==========================================================================
   MinatoVerse OTT · /search page logic
   --------------------------------------------------------------------------
   Reads the server-rendered results (and the JSON bootstrap), then adds:
   live filtering (genre / year / quality / sort), URL sync, "load more"
   pagination, empty + error states and inline-mode hints.
   Relies on window.MinatoOTT from ott_home.js (cards, My List, sheet, toast).
   ========================================================================== */
(function () {
    "use strict";

    const API = window.MinatoOTT || {};
    const CONFIG = API.CONFIG || {};
    const PATHS = API.PATHS || {};
    const BOOT = API.BOOT || {};
    const LIMIT = parseInt((CONFIG.limits || {}).search, 10) || 24;
    const REDUCED = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

    const state = {
        query: BOOT.query || "",
        genre: BOOT.genre || "",
        year: String(BOOT.year || ""),
        quality: BOOT.quality || "",
        sort: BOOT.sort || "relevance",
        page: 1,
        total: BOOT.total || 0,
        shown: (BOOT.results || []).length,
        loading: false,
        facets: BOOT.facets || { genres: [], years: [], qualities: [] }
    };

    const el = {
        grid: document.getElementById("otGrid"),
        count: document.getElementById("otSearchCount"),
        shown: document.getElementById("otShown"),
        total: document.getElementById("otTotal"),
        empty: document.getElementById("otEmpty"),
        emptyTitle: document.getElementById("otEmptyTitle"),
        emptyText: document.getElementById("otEmptyText"),
        loadMore: document.getElementById("otLoadMore"),
        loadHint: document.getElementById("otLoadHint"),
        input: document.getElementById("otSearchInput"),
        clear: document.getElementById("otSearchClear")
    };

    function toast(message) { if (API.toast) { API.toast(message); } }

    function statusLine(text) {
        if (!el.loadHint) { return; }
        let node = document.getElementById("otSearchLive");
        if (!node) {
            node = document.createElement("span");
            node.id = "otSearchLive";
            node.className = "ot-search-status";
            node.innerHTML = '<span class="ot-spinner" aria-hidden="true"></span><span id="otSearchLiveText"></span>';
            el.loadHint.parentNode.insertBefore(node, el.loadHint);
        }
        const label = node.querySelector("#otSearchLiveText");
        if (!text) { node.hidden = true; return; }
        node.hidden = false;
        if (label) { label.textContent = text; }
    }

    function skeletons(count) {
        if (!el.grid) { return; }
        let html = '<div class="ot-skel-grid" aria-hidden="true">';
        for (let i = 0; i < (count || 12); i += 1) { html += '<span class="ot-skel"></span>'; }
        el.grid.insertAdjacentHTML("beforebegin", html + "</div>");
    }

    function removeSkeletons() {
        document.querySelectorAll(".ot-skel-grid").forEach((node) => node.remove());
    }

    function params(extra) {
        const search = new URLSearchParams();
        if (state.query) { search.set("q", state.query); }
        if (state.genre) { search.set("genre", state.genre); }
        if (state.year) { search.set("year", state.year); }
        if (state.quality) { search.set("quality", state.quality); }
        if (state.sort) { search.set("sort", state.sort); }
        if (state.page > 1) { search.set("page", String(state.page)); }
        search.set("limit", String(LIMIT));
        Object.keys(extra || {}).forEach((key) => search.set(key, extra[key]));
        return search.toString();
    }

    function syncURL() {
        try {
            const search = new URLSearchParams();
            if (state.query) { search.set("q", state.query); }
            if (state.genre) { search.set("genre", state.genre); }
            if (state.year) { search.set("year", state.year); }
            if (state.quality) { search.set("quality", state.quality); }
            if (state.sort && state.sort !== "relevance") { search.set("sort", state.sort); }
            const query = search.toString();
            window.history.replaceState({}, "", (PATHS.searchPage || "/search") + (query ? "?" + query : ""));
        } catch (e) { /* file:// or blocked history */ }
    }

    function renderResults(results, append) {
        if (!el.grid) { return; }
        const markup = (results || []).map((card) => API.cardMarkup(API.cardFromJSON(card), {})).join("");
        if (append) { el.grid.insertAdjacentHTML("beforeend", markup); }
        else { el.grid.innerHTML = markup; }
        if (API.hydrate) { API.hydrate(el.grid); }
    }

    function updateCounters() {
        if (el.count) { el.count.textContent = state.total; }
        if (el.shown) { el.shown.textContent = state.shown; }
        if (el.total) { el.total.textContent = state.total; }
        const hasResults = state.shown > 0;
        if (el.empty) { el.empty.hidden = hasResults; }
        if (el.loadMore) { el.loadMore.hidden = !hasResults || state.shown >= state.total; }
        if (el.emptyTitle && !hasResults) {
            el.emptyTitle.textContent = state.query
                ? "Nothing matched “" + state.query + "”"
                : "No titles here yet";
        }
        if (el.emptyText && !hasResults) {
            el.emptyText.textContent = state.query
                ? "Try a shorter title, drop the year, or open the bot and ask for it — new uploads appear here automatically."
                : "Filters are too narrow — reset them to see the whole library.";
        }
    }

    function renderFacets(facets) {
        if (!facets || !el.grid) { return; }
        const groups = {
            genre: facets.genres || [],
            quality: facets.qualities || [],
            year: (facets.years || []).map((year) => ({ name: String(year), count: 0 }))
        };
        Object.keys(groups).forEach((key) => {
            const container = document.querySelector('.ot-filter-group[aria-label="' + key.charAt(0).toUpperCase() + key.slice(1) + '"]');
            if (!container) { return; }
            const activeValue = key === "genre" ? state.genre : (key === "quality" ? state.quality : state.year);
            const buttons = groups[key].map((facet) => {
                const value = String(facet.name);
                const active = activeValue === value ? " is-active" : "";
                const count = facet.count && key !== "year" ? " <em>" + facet.count + "</em>" : "";
                return '<button class="ot-filter' + active + '" type="button" data-' + key + '="' +
                    value.replace(/"/g, "&quot;") + '">' + value + count + "</button>";
            }).join("");
            const allActive = activeValue ? "" : " is-active";
            const all = '<button class="ot-filter' + allActive + '" type="button" data-' + key + '="">All</button>';
            container.innerHTML = '<span class="ot-filter-label">' + key.charAt(0).toUpperCase() + key.slice(1) + "</span>" + all + buttons;
        });
    }

    async function load(options) {
        const opts = options || {};
        if (state.loading) { return; }
        state.loading = true;
        if (opts.append) { state.page += 1; } else { state.page = 1; }
        if (el.loadMore) { el.loadMore.disabled = true; }
        statusLine(opts.append ? "Loading more titles…" : "Searching the library…");
        if (!opts.append) { skeletons(12); }
        try {
            const response = await window.fetch((PATHS.search || "/api/ott/search") + "?" + params(), {
                headers: { Accept: "application/json" }
            });
            if (!response.ok) { throw new Error("HTTP " + response.status); }
            const payload = await response.json();
            removeSkeletons();
            if (!payload || !payload.ok) { throw new Error("payload"); }
            state.total = payload.total || 0;
            const results = payload.results || [];
            state.shown = opts.append ? state.shown + results.length : results.length;
            renderResults(results, !!opts.append);
            if (payload.facets) { state.facets = payload.facets; renderFacets(payload.facets); }
            updateCounters();
            syncURL();
            statusLine("");
            if (opts.append && !results.length) { toast("No more results"); }
        } catch (e) {
            removeSkeletons();
            statusLine("");
            if (!opts.append) {
                if (el.empty) { el.empty.hidden = false; }
                if (el.emptyTitle) { el.emptyTitle.textContent = "Search is unavailable right now"; }
                if (el.emptyText) { el.emptyText.textContent = "The library did not answer. Check the bot's web server and try again."; }
            } else {
                toast("Could not load more");
            }
        } finally {
            state.loading = false;
            if (el.loadMore) {
                el.loadMore.disabled = false;
                el.loadMore.textContent = state.shown >= state.total ? "All results shown" : "Load more";
            }
        }
    }

    function activeChips() {
        document.querySelectorAll(".ot-filter").forEach((button) => {
            const value = button.dataset.genre !== undefined ? button.dataset.genre
                : (button.dataset.quality !== undefined ? button.dataset.quality
                    : (button.dataset.year !== undefined ? button.dataset.year
                        : (button.dataset.sort !== undefined ? button.dataset.sort : null)));
            if (value === null) { return; }
            const key = button.dataset.genre !== undefined ? "genre"
                : (button.dataset.quality !== undefined ? "quality"
                    : (button.dataset.year !== undefined ? "year" : "sort"));
            let current = state[key];
            if (key === "sort" && !current) { current = "relevance"; }
            if (key === "sort" && value === "relevance" && !state.sort) { button.classList.add("is-active"); return; }
            button.classList.toggle("is-active", String(current) === String(value));
        });
    }

    function resetFilters() {
        state.genre = ""; state.quality = ""; state.year = ""; state.sort = "relevance";
        activeChips();
        load({});
    }

    function wire() {
        document.addEventListener("click", (event) => {
            const button = event.target.closest(".ot-filter");
            if (!button) { return; }
            if (button.dataset.genre !== undefined) { state.genre = button.dataset.genre; state.page = 1; load({}); }
            else if (button.dataset.quality !== undefined) { state.quality = button.dataset.quality; state.page = 1; load({}); }
            else if (button.dataset.year !== undefined) { state.year = button.dataset.year; state.page = 1; load({}); }
            else if (button.dataset.sort !== undefined) { state.sort = button.dataset.sort; state.page = 1; load({}); }
            activeChips();
        });

        const reset = document.getElementById("otFilterReset");
        if (reset) { reset.addEventListener("click", resetFilters); }
        const emptyReset = document.getElementById("otEmptyReset");
        if (emptyReset) { emptyReset.addEventListener("click", () => { if (el.input) { el.input.value = ""; } state.query = ""; resetFilters(); }); }
        if (el.loadMore) { el.loadMore.addEventListener("click", () => load({ append: true })); }

        if (el.clear && el.input) {
            el.clear.addEventListener("click", () => {
                el.input.value = ""; el.clear.hidden = true;
                state.query = ""; load({});
                el.input.focus();
            });
        }
        const form = document.getElementById("otSearchForm");
        if (form) {
            form.addEventListener("submit", (event) => {
                event.preventDefault();
                state.query = el.input ? el.input.value.trim() : "";
                load({});
            });
        }
        if (el.input) {
            let timer = null;
            el.input.addEventListener("input", () => {
                if (el.clear) { el.clear.hidden = !el.input.value; }
                window.clearTimeout(timer);
                timer = window.setTimeout(() => {
                    state.query = el.input.value.trim();
                    if (state.query.length === 1) { return; }  // wait for a real word
                    load({});
                }, 420);
            });
        }
        window.addEventListener("popstate", () => window.location.reload());
        if (!REDUCED && window.scrollY === 0 && state.query) {
            // Deep link with a query: keep the results in view.
            window.setTimeout(() => {
                const target = document.getElementById("otResults");
                if (target) { target.scrollIntoView({ block: "start", behavior: "auto" }); }
            }, 60);
        }
    }

    function init() {
        if (!document.body.classList.contains("ot-body-search")) { return; }
        wire();
        updateCounters();
        activeChips();
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
}());
