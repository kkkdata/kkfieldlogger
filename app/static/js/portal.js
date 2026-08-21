document.addEventListener("DOMContentLoaded", () => {
    const sidebar = document.getElementById("portal-sidebar");
    const sidebarToggle = document.querySelector("[data-sidebar-toggle]");
    const sidebarScrim = document.querySelector("[data-sidebar-close]");
    const closeSidebar = () => {
        document.body.classList.remove("sidebar-open");
        if (sidebarToggle instanceof HTMLElement) {
            sidebarToggle.setAttribute("aria-expanded", "false");
        }
        if (sidebarScrim instanceof HTMLElement) {
            sidebarScrim.hidden = true;
        }
    };
    const openSidebar = () => {
        document.body.classList.add("sidebar-open");
        if (sidebarToggle instanceof HTMLElement) {
            sidebarToggle.setAttribute("aria-expanded", "true");
        }
        if (sidebarScrim instanceof HTMLElement) {
            sidebarScrim.hidden = false;
        }
    };
    if (sidebar && sidebarToggle instanceof HTMLElement) {
        sidebarToggle.addEventListener("click", () => {
            if (document.body.classList.contains("sidebar-open")) {
                closeSidebar();
                return;
            }
            openSidebar();
        });
        if (sidebarScrim instanceof HTMLElement) {
            sidebarScrim.addEventListener("click", closeSidebar);
        }
        sidebar.querySelectorAll(".nav-link").forEach((link) => {
            link.addEventListener("click", () => {
                if (window.matchMedia("(max-width: 960px)").matches) {
                    closeSidebar();
                }
            });
        });
        document.addEventListener("keydown", (event) => {
            if (event.key === "Escape" && document.body.classList.contains("sidebar-open")) {
                closeSidebar();
            }
        });
    }

    const mediaUnavailableFallback = "/static/img/media-unavailable.svg";
    const applyImageFallback = (image) => {
        if (!(image instanceof HTMLImageElement)) {
            return;
        }
        const finalFallback = image.dataset.finalFallbackSrc || mediaUnavailableFallback;
        const candidateFallback = image.dataset.fallbackSrc || "";
        if (candidateFallback && image.dataset.fallbackApplied !== "source") {
            image.dataset.fallbackApplied = "source";
            image.src = candidateFallback;
            return;
        }
        if (finalFallback && image.dataset.fallbackApplied !== "final") {
            image.dataset.fallbackApplied = "final";
            image.classList.add("is-media-unavailable");
            image.src = finalFallback;
        }
    };
    document.querySelectorAll("img[data-fallback-src], img[data-final-fallback-src]").forEach((image) => {
        if (!(image instanceof HTMLImageElement)) {
            return;
        }
        image.addEventListener("error", () => applyImageFallback(image));
        if (image.complete && image.naturalWidth === 0) {
            applyImageFallback(image);
        }
    });

    const modal = document.getElementById("image-modal");
    const modalImage = document.getElementById("modal-image");
    const modalTitle = document.getElementById("modal-title");
    const modalInfo = document.getElementById("modal-info");
    const modalPanel = document.getElementById("modal-panel");
    const previewFrame = modal ? modal.querySelector(".image-preview-frame") : null;
    const getImageNodes = () =>
        Array.from(document.querySelectorAll("[data-image-src][data-image-title]")).sort((left, right) => {
            const leftIndex = Number.parseInt(String(left.dataset.imageIndex || ""), 10);
            const rightIndex = Number.parseInt(String(right.dataset.imageIndex || ""), 10);
            if (Number.isFinite(leftIndex) && Number.isFinite(rightIndex)) {
                return leftIndex - rightIndex;
            }
            return 0;
        });
    const prevButtons = Array.from(document.querySelectorAll("[data-image-prev]"));
    const nextButtons = Array.from(document.querySelectorAll("[data-image-next]"));
    let zoom = 1;
    let offsetX = 0;
    let offsetY = 0;
    let currentIndex = -1;
    let dragState = null;

    if (!modal || !modalImage || !modalTitle || !modalInfo || !modalPanel || !previewFrame) {
        return;
    }

    const applyTransform = () => {
        modalImage.style.transform = `translate(${offsetX}px, ${offsetY}px) scale(${zoom})`;
        modalImage.classList.toggle("is-zoomed", zoom > 1.01);
        previewFrame.classList.toggle("is-pannable", zoom > 1.01);
    };

    const resetView = () => {
        zoom = 1;
        offsetX = 0;
        offsetY = 0;
        dragState = null;
        applyTransform();
    };

    const closeModal = () => {
        modal.hidden = true;
        modalImage.src = "";
        currentIndex = -1;
        resetView();
    };

    const openImageAtIndex = (index) => {
        const imageNodes = getImageNodes();
        const target = imageNodes[index];
        if (!(target instanceof HTMLElement)) {
            return;
        }
        currentIndex = index;
        modalImage.src = target.dataset.imageSrc || "";
        modalImage.dataset.fallbackSrc = target.dataset.imageFallbackSrc || "";
        modalImage.dataset.finalFallbackSrc = target.dataset.imageFinalFallbackSrc || mediaUnavailableFallback;
        delete modalImage.dataset.fallbackApplied;
        modalImage.classList.remove("is-media-unavailable");
        modalTitle.textContent = target.dataset.imageTitle || "Preview";
        modalInfo.textContent = target.dataset.imageInfo || "";
        resetView();
        modal.hidden = false;
    };

    const moveToImage = (direction) => {
        const imageNodes = getImageNodes();
        if (imageNodes.length === 0) {
            return;
        }
        if (currentIndex === -1) {
            currentIndex = imageNodes.findIndex((node) => (node instanceof HTMLElement ? node.dataset.imageSrc : "") === modalImage.src);
        }
        const baseIndex = currentIndex >= 0 ? currentIndex : 0;
        const nextIndex = (baseIndex + direction + imageNodes.length) % imageNodes.length;
        openImageAtIndex(nextIndex);
    };

    const nudgeZoom = (delta) => {
        zoom = Math.max(0.5, Math.min(4, zoom + delta));
        if (zoom <= 1.01) {
            offsetX = 0;
            offsetY = 0;
        }
        applyTransform();
    };

    getImageNodes().forEach((node, index) => {
        node.addEventListener("click", () => {
            openImageAtIndex(index);
        });
    });

    document.querySelectorAll("[data-close-modal]").forEach((node) => {
        node.addEventListener("click", closeModal);
    });

    document.querySelectorAll("[data-zoom]").forEach((node) => {
        node.addEventListener("click", () => {
            nudgeZoom(Number(node.dataset.zoom));
        });
    });

    document.querySelectorAll("[data-zoom-reset]").forEach((node) => {
        node.addEventListener("click", resetView);
    });

    prevButtons.forEach((node) => {
        node.addEventListener("click", () => moveToImage(-1));
    });

    nextButtons.forEach((node) => {
        node.addEventListener("click", () => moveToImage(1));
    });

    document.querySelectorAll("[data-fullscreen-toggle]").forEach((node) => {
        node.addEventListener("click", async () => {
            if (!document.fullscreenElement) {
                await modalPanel.requestFullscreen();
                return;
            }
            await document.exitFullscreen();
        });
    });

    previewFrame.addEventListener(
        "wheel",
        (event) => {
            if (modal.hidden) {
                return;
            }
            event.preventDefault();
            const delta = event.deltaY < 0 ? 0.12 : -0.12;
            nudgeZoom(delta);
        },
        { passive: false },
    );

    modalImage.addEventListener("pointerdown", (event) => {
        if (zoom <= 1.01) {
            return;
        }
        dragState = {
            startX: event.clientX,
            startY: event.clientY,
            baseX: offsetX,
            baseY: offsetY,
        };
        modalImage.setPointerCapture(event.pointerId);
    });

    modalImage.addEventListener("pointermove", (event) => {
        if (!dragState) {
            return;
        }
        offsetX = dragState.baseX + (event.clientX - dragState.startX);
        offsetY = dragState.baseY + (event.clientY - dragState.startY);
        applyTransform();
    });

    const clearDrag = () => {
        dragState = null;
    };

    modalImage.addEventListener("pointerup", clearDrag);
    modalImage.addEventListener("pointercancel", clearDrag);
    modalImage.addEventListener("pointerleave", clearDrag);

    document.addEventListener("keydown", async (event) => {
        if (modal.hidden) {
            return;
        }
        if (event.key === "Escape") {
            event.preventDefault();
            if (document.fullscreenElement) {
                await document.exitFullscreen();
                return;
            }
            closeModal();
            return;
        }
        if (event.key === "ArrowLeft") {
            event.preventDefault();
            moveToImage(-1);
            return;
        }
        if (event.key === "ArrowRight") {
            event.preventDefault();
            moveToImage(1);
            return;
        }
        if (event.key === "+" || event.key === "=") {
            event.preventDefault();
            nudgeZoom(0.12);
            return;
        }
        if (event.key === "-") {
            event.preventDefault();
            nudgeZoom(-0.12);
        }
    });
});
