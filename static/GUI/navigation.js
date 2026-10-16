(() => {
    document.addEventListener("click", (event) => {
        const link = event.target?.closest?.("a[href]");
        if (link && !(link.getAttribute("href") || "").startsWith("#")) {
            event.preventDefault();
        }
    }, true);

    // Receive drops explicitly instead of letting the engine navigate to a file.
    document.addEventListener("dragover", (event) => event.preventDefault(), true);
    document.addEventListener("drop", (event) => {
        const transfer = event.dataTransfer;
        const types = Array.from(transfer?.types || []);
        const isInput = Boolean(event.target?.closest?.("input, textarea"));
        const text = transfer?.getData("text/plain")?.trim() || "";
        const hasExternalResource = types.includes("Files") || types.includes("text/uri-list");
        const isUrl = /^(?:[a-z][a-z0-9+.-]*:|\/\/)/i.test(text);
        if (hasExternalResource || (!isInput && isUrl)) {
            event.preventDefault();
        }
    }, true);
})();
