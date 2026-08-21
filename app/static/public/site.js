(function () {
    "use strict";

    const STORAGE_KEY = "kk-public-site-lang";
    const SUPPORTED = ["en", "zh", "es"];
    const LOCALE_MAP = {
        en: "en",
        zh: "zh-CN",
        es: "es",
    };

    function normalizeLanguage(value) {
        const raw = String(value || "").trim().toLowerCase();
        if (!raw) {
            return "en";
        }
        if (raw.startsWith("zh")) {
            return "zh";
        }
        if (raw.startsWith("es")) {
            return "es";
        }
        return SUPPORTED.includes(raw) ? raw : "en";
    }

    function readInitialLanguage() {
        const params = new URLSearchParams(window.location.search);
        const queryLanguage = params.get("lang");
        if (queryLanguage) {
            return normalizeLanguage(queryLanguage);
        }
        const saved = window.localStorage.getItem(STORAGE_KEY);
        if (saved) {
            return normalizeLanguage(saved);
        }
        return normalizeLanguage(window.navigator.language || "en");
    }

    let currentLanguage = readInitialLanguage();

    function pickFromDataset(node, prefix) {
        const suffix = currentLanguage.charAt(0).toUpperCase() + currentLanguage.slice(1);
        const key = `${prefix}${suffix}`;
        const fallbackKey = `${prefix}En`;
        return node.dataset[key] || node.dataset[fallbackKey] || "";
    }

    function applyLanguage(language) {
        currentLanguage = normalizeLanguage(language);
        window.localStorage.setItem(STORAGE_KEY, currentLanguage);
        document.documentElement.lang = LOCALE_MAP[currentLanguage];
        document.documentElement.dataset.lang = currentLanguage;

        document.querySelectorAll("[data-l10n]").forEach((node) => {
            node.hidden = node.dataset.l10n !== currentLanguage;
        });

        document.querySelectorAll("[data-ph-en]").forEach((node) => {
            node.setAttribute("placeholder", pickFromDataset(node, "ph"));
        });

        const body = document.body;
        if (body) {
            const title = pickFromDataset(body, "title");
            const description = pickFromDataset(body, "description");
            if (title) {
                document.title = title;
            }
            if (description) {
                const meta = document.querySelector("meta[name='description']");
                if (meta) {
                    meta.setAttribute("content", description);
                }
            }
        }

        document.querySelectorAll("[data-set-lang]").forEach((button) => {
            button.classList.toggle("active", button.dataset.setLang === currentLanguage);
        });
    }

    function bindLanguageButtons() {
        document.querySelectorAll("[data-set-lang]").forEach((button) => {
            button.addEventListener("click", () => {
                applyLanguage(button.dataset.setLang);
            });
        });
    }

    window.KKSite = {
        getLanguage: function () {
            return currentLanguage;
        },
        setLanguage: applyLanguage,
        pick: function (translations) {
            return translations[currentLanguage] || translations.en || "";
        },
    };

    document.addEventListener("DOMContentLoaded", function () {
        bindLanguageButtons();
        applyLanguage(currentLanguage);
    });
})();
