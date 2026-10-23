(() => {
  "use strict";

  const root = document.querySelector("[data-map-app]");
  const languageButtons = [...document.querySelectorAll("button[data-language]")];
  const locale = () => document.documentElement.lang === "it" ? "it-IT" : "en-GB";
  let liveMapController = null;
  let selectionGeneration = 0;

  function buildGoogleMapsUrl(coordinates) {
    if (!coordinates || typeof coordinates.latitude !== "number" || typeof coordinates.longitude !== "number") {
      return null;
    }
    const { latitude, longitude } = coordinates;
    if (!Number.isFinite(latitude) || !Number.isFinite(longitude)
      || latitude < -90 || latitude > 90 || longitude < -180 || longitude > 180) {
      return null;
    }
    const query = new URLSearchParams({ api: "1", query: `${latitude},${longitude}` });
    return `https://www.google.com/maps/search/?${query.toString()}`;
  }

  window.SporaMeteoNavigation = { buildGoogleMapsUrl };

  function beginSelectionIntent() {
    selectionGeneration += 1;
    return selectionGeneration;
  }

  function date(value) {
    return new Intl.DateTimeFormat(locale(), {
      day: "numeric",
      month: "long",
      year: "numeric",
      timeZone: "Europe/Rome",
    }).format(new Date(`${value}T12:00:00Z`));
  }

  function number(value) {
    return new Intl.NumberFormat(locale(), {
      maximumFractionDigits: 1,
      minimumFractionDigits: 1,
    }).format(value);
  }

  function measurement(value, unit) {
    return `${number(value)} ${unit}`;
  }

  function fullDate(value) {
    const display = new Intl.DateTimeFormat(locale(), {
      weekday: "long", day: "numeric", month: "long", year: "numeric",
      timeZone: "Europe/Rome",
    }).format(new Date(`${value}T12:00:00Z`));
    return display.charAt(0).toLocaleUpperCase(locale()) + display.slice(1);
  }

  function dateTime(value) {
    if (!value) return "";
    return new Intl.DateTimeFormat(locale(), {
      dateStyle: "medium",
      timeStyle: "short",
      timeZone: "Europe/Rome",
    }).format(new Date(value));
  }

  function compactDateRange(values, formatter) {
    if (!values.length) return "";
    const first = formatter(values[0]);
    const last = formatter(values[values.length - 1]);
    return first === last ? first : `${first} - ${last}`;
  }

  window.SporaMeteoFormatting = {
    formatDate: date,
    formatNumber: number,
    formatMeasurement: measurement,
  };

  function setLanguage(language) {
    if (!/^(it|en)$/.test(language)) return;
    document.cookie = `sporameteo_language=${language};path=/;max-age=31536000;samesite=lax`;
    const next = new URL(window.location.href);
    next.searchParams.delete("lang");
    window.location.assign(next);
  }

  languageButtons.forEach((button) => button.addEventListener("click", () => setLanguage(button.dataset.language)));
  if (!root) return;
  const labels = JSON.parse(document.getElementById("ui-labels")?.textContent || "{}");
  const estimateColors = [0, "#fff9f0", 60, "#e5ac43", 65, "#24958b", 80, "#7851b5", 90, "#432475"];
  const colorKey = document.querySelector(".lg-scale");
  if (colorKey) {
    colorKey.replaceChildren();
    for (let index = 0; index < estimateColors.length; index += 2) {
      const chip = document.createElement("span");
      chip.className = "lg-chip";
      const swatch = document.createElement("i");
      swatch.style.background = estimateColors[index + 1];
      chip.append(swatch, String(estimateColors[index]) + (index === 8 ? "+" : ""));
      colorKey.append(chip);
    }
    document.querySelector(".lg-full .legend-group")?.append(colorKey.cloneNode(true));
  }

  function setAssessmentLabel(value) {
    const assessment = document.querySelector(".date-assess");
    if (!assessment) return;
    const strong = document.createElement("strong");
    strong.textContent = value;
    assessment.replaceChildren(`${labels.assessment}: `, strong);
  }

  function shortAssessmentDate(value) {
    return new Intl.DateTimeFormat(locale(), {
      day: "numeric", month: "short", year: "numeric", timeZone: "Europe/Rome",
    }).format(new Date(`${value}T12:00:00Z`));
  }

  function updateDateMetadata(features, loading = false) {
    const assessment = document.querySelector(".date-assess");
    const calculated = document.querySelector("[data-date-update]");
    if (!assessment || !calculated) return;
    if (loading) {
      setAssessmentLabel(labels.map_loading || labels.loading);
      calculated.hidden = true;
      calculated.textContent = "";
      return;
    }
    const values = Array.isArray(features) ? features : [];
    const records = values
      .map((feature) => ({
        assessment: feature.properties?.assessment_date,
        calculated: feature.properties?.calculated_at,
      }))
      .filter((record) => record.assessment || record.calculated);
    const assessmentDates = [...new Set(records.map((record) => record.assessment).filter(Boolean))].sort();
    setAssessmentLabel(assessmentDates.length
      ? compactDateRange(assessmentDates, shortAssessmentDate)
      : "-");
    const calculatedAt = [...new Set(records.map((record) => record.calculated).filter(Boolean))].sort();
    calculated.hidden = !calculatedAt.length;
    calculated.textContent = calculatedAt.length
      ? `${labels.map_calculated_at}: ${compactDateRange(calculatedAt, dateTime)}`
      : "";
  }

  const params = new URLSearchParams(window.location.search);
  const geolocationStateKey = "sporameteo.currentGeolocation";
  const locationQueryKeys = [
    "square",
    "center",
    "zoom",
    "point_latitude",
    "point_longitude",
    "representative_latitude",
    "representative_longitude",
    "point_source",
  ];
  const oldGeolocationUrl = params.get("point_source") === "geolocation";
  const hasExplicitLocationUrl = !oldGeolocationUrl && locationQueryKeys.some((key) => params.has(key));

  function readGeolocationState() {
    if (hasExplicitLocationUrl) return null;
    try {
      const stored = JSON.parse(window.sessionStorage.getItem(geolocationStateKey) || "null");
      if (stored?.source !== "geolocation" || !stored.point) return null;
      if (![stored.point.latitude, stored.point.longitude].every(Number.isFinite)) return null;
      return stored;
    } catch (error) {
      return null;
    }
  }

  function clearStoredGeolocation() {
    try {
      window.sessionStorage.removeItem(geolocationStateKey);
    } catch (error) {
      return;
    }
  }

  function removeLocationQueryState() {
    const next = new URL(window.location.href);
    let changed = false;
    locationQueryKeys.forEach((key) => {
      if (next.searchParams.has(key)) {
        next.searchParams.delete(key);
        changed = true;
      }
    });
    if (changed) window.history.replaceState({}, "", next);
  }

  if (oldGeolocationUrl) removeLocationQueryState();
  const storedGeolocation = readGeolocationState();
  const initialPoint = storedGeolocation?.point || {
    latitude: Number(oldGeolocationUrl ? "NaN" : params.get("point_latitude") || root.dataset.pointLatitude || "NaN"),
    longitude: Number(oldGeolocationUrl ? "NaN" : params.get("point_longitude") || root.dataset.pointLongitude || "NaN"),
  };
  const initialRepresentative = storedGeolocation?.representative || {
    latitude: Number(oldGeolocationUrl ? "NaN" : params.get("representative_latitude") || root.dataset.representativeLatitude || "NaN"),
    longitude: Number(oldGeolocationUrl ? "NaN" : params.get("representative_longitude") || root.dataset.representativeLongitude || "NaN"),
  };
  const state = {
    square: storedGeolocation?.square || (oldGeolocationUrl ? "" : params.get("square") || root.dataset.selectedSquare || ""),
    date: root.dataset.targetDate || params.get("date") || "",
    resultId: params.get("result") || root.dataset.resultId || "",
    assessment: root.dataset.assessmentDate || params.get("assessment") || "",
    updateId: params.get("update") || root.dataset.updateId || "",
    center: storedGeolocation?.center || (oldGeolocationUrl ? "" : params.get("center") || root.dataset.mapCenter || ""),
    zoom: Number(storedGeolocation?.zoom || (oldGeolocationUrl ? root.dataset.mapZoom || "11" : params.get("zoom") || root.dataset.mapZoom || "11")),
    point: initialPoint,
    pointName: "",
    representative: initialRepresentative,
    detailRepresentative: null,
    detailRepresentativeSquare: "",
    detailRepresentativeDate: "",
    detailRepresentativeUpdate: "",
    pointSource: storedGeolocation ? "geolocation" : (oldGeolocationUrl ? "" : params.get("point_source") || root.dataset.selectionSource || ""),
    panel: params.get("panel") || "",
    details: params.has("details") ? params.get("details") === "true" : root.dataset.detailsOpen === "true",
    sheetHeight: params.get("sheet") || "short",
    background: params.get("background") || "light",
    overlay: params.get("overlay") !== "false",
    opacity: Number(params.get("opacity") || "60"),
    // The layers box starts folded. Only an explicit unfold in the address keeps it open.
    foldedLayers: params.get("foldedLayers") !== "false",
    foldedLegend: params.get("foldedLegend") === "true",
    foldedDates: params.get("foldedDates") === "true",
    legendMore: params.get("legendMore") === "true",
  };
  if (!["short", "half", "tall"].includes(state.sheetHeight)) state.sheetHeight = "short";
  if (!["light", "contrast"].includes(state.background)) state.background = "light";
  state.opacity = Number.isFinite(state.opacity) ? Math.min(100, Math.max(0, state.opacity)) : 60;
  state.zoom = Number.isFinite(state.zoom) ? Math.min(19, Math.max(1, state.zoom)) : 11;
  if (!["layers", "legend"].includes(state.panel)) state.panel = "";
  if (!Number.isFinite(state.point.latitude) || !Number.isFinite(state.point.longitude)) {
    state.point = null;
  }
  if (!Number.isFinite(state.representative.latitude) || !Number.isFinite(state.representative.longitude)) {
    state.representative = null;
  }

  const toastTarget = document.querySelector("[data-toast]");
  let toastTimer;
  function toast(message) {
    if (!toastTarget) return;
    toastTarget.textContent = message;
    toastTarget.classList.add("is-visible");
    window.clearTimeout(toastTimer);
    toastTimer = window.setTimeout(() => toastTarget.classList.remove("is-visible"), 2600);
  }

  function verifiedRepresentative() {
    if (!state.representative
      || state.detailRepresentativeSquare !== state.square
      || state.detailRepresentativeDate !== state.date
      || !state.detailRepresentativeUpdate
      || String(state.detailRepresentativeUpdate) !== String(state.updateId || "")) {
      return null;
    }
    return state.representative;
  }

  function navigationCoordinates() {
    if (state.pointSource === "centre") return verifiedRepresentative();
    return state.point || verifiedRepresentative();
  }

  function updateNavigationAction() {
    const coordinates = navigationCoordinates();
    const url = root.dataset.fixture ? null : buildGoogleMapsUrl(coordinates);
    document.querySelectorAll("[data-navigation]").forEach((control) => {
      control.disabled = !url;
      control.dataset.navigationUrl = url || "";
      control.setAttribute("aria-disabled", String(!url));
    });
    document.querySelectorAll("[data-navigation-status]").forEach((status) => {
      const message = url && state.pointSource === "centre"
        ? labels.navigation_representative
        : url ? "" : labels.navigation_unavailable;
      status.hidden = !message;
      status.textContent = message;
    });
  }

  function createPlaceSearch() {
    const form = document.querySelector("[data-search-form]");
    const input = document.querySelector("[data-place-search]");
    const resultsBox = document.querySelector("[data-search-results]");
    const status = document.querySelector("[data-search-status]");
    const locationButton = document.querySelector("[data-location]");
    if (!form || !input || !resultsBox) return null;

    let generation = 0;
    let timer = null;
    let request = null;
    let items = [];
    let activeIndex = -1;
    const searchCache = new Map();
    const normalizeSearch = (value) => value.normalize("NFKC").trim().replace(/\s+/g, " ").toLocaleLowerCase();

    function rememberSearch(key, results) {
      searchCache.delete(key);
      searchCache.set(key, { results, expires: Date.now() + (results.length ? 1_800_000 : 60_000) });
      while (searchCache.size > 100) searchCache.delete(searchCache.keys().next().value);
    }

    function cachedSuggestions(query, language) {
      const found = new Map();
      for (const [key, entry] of searchCache) {
        if (entry.expires <= Date.now()) {
          searchCache.delete(key);
          continue;
        }
        if (!key.startsWith(`${language}|`)) continue;
        for (const item of entry.results) {
          const name = normalizeSearch(item.name || "");
          if (name.startsWith(query) || name.split(" ").some((word) => word.startsWith(query))) {
            found.set(JSON.stringify([item.name, item.context, item.latitude, item.longitude]), item);
          }
        }
      }
      return [...found.values()].slice(0, 8);
    }

    function appendAttribution() {
      const attribution = document.createElement("div");
      attribution.className = "search-attribution";
      attribution.setAttribute("role", "note");
      attribution.append(`${labels.search_attribution || "Place names from"} `);
      [
        ["Photon", "https://photon.komoot.io/"],
        ["OpenStreetMap", "https://www.openstreetmap.org/copyright"],
      ].forEach(([text, url], index) => {
        if (index) attribution.append(" · ");
        const link = document.createElement("a");
        link.href = url;
        link.rel = "noreferrer";
        link.textContent = text;
        attribution.append(link);
      });
      resultsBox.append(attribution);
    }

    function announce(message) {
      if (status) status.textContent = message || "";
    }

    function hideResults() {
      resultsBox.hidden = true;
      input.setAttribute("aria-expanded", "false");
      input.removeAttribute("aria-activedescendant");
      activeIndex = -1;
    }

    function cancelSearch() {
      window.clearTimeout(timer);
      timer = null;
      request?.abort();
      request = null;
      generation += 1;
      items = [];
      activeIndex = -1;
      resultsBox.replaceChildren();
    }

    function closeResults() {
      cancelSearch();
      hideResults();
    }

    function setActive(index) {
      activeIndex = items.length ? (index + items.length) % items.length : -1;
      const options = [...resultsBox.querySelectorAll("[role=option]")];
      options.forEach((option, optionIndex) => {
        const selected = optionIndex === activeIndex;
        option.setAttribute("aria-selected", String(selected));
      });
      if (activeIndex < 0) {
        input.removeAttribute("aria-activedescendant");
        return;
      }
      const option = options[activeIndex];
      if (!option) return;
      input.setAttribute("aria-activedescendant", option.id);
      option.scrollIntoView({ block: "nearest" });
    }

    function sameResult(first, second) {
      return first.name === second.name
        && Number(first.latitude) === Number(second.latitude)
        && Number(first.longitude) === Number(second.longitude);
    }

    function renderResults(nextItems) {
      const previousActive = items[activeIndex];
      activeIndex = -1;
      input.removeAttribute("aria-activedescendant");
      items = Array.isArray(nextItems) ? nextItems : [];
      resultsBox.replaceChildren();
      if (!items.length) {
        const empty = document.createElement("div");
        empty.className = "result-empty";
        empty.setAttribute("role", "option");
        empty.setAttribute("aria-disabled", "true");
        const title = document.createElement("strong");
        title.textContent = labels.search_no_results;
        empty.append(title);
        resultsBox.append(empty);
        appendAttribution();
        resultsBox.hidden = false;
        input.setAttribute("aria-expanded", "true");
        announce(labels.search_no_results);
        return;
      }
      items.forEach((item, index) => {
        const option = document.createElement("button");
        option.type = "button";
        option.className = "result";
        option.id = `place-result-${generation}-${index}`;
        option.setAttribute("role", "option");
        option.setAttribute("aria-selected", "false");
        const name = document.createElement("span");
        name.className = "result-name";
        name.textContent = item.name || "";
        option.append(name);
        if (item.context) {
          const context = document.createElement("span");
          context.className = "result-context";
          context.textContent = item.context;
          option.append(context);
        }
        option.addEventListener("click", () => choose(item));
        resultsBox.append(option);
      });
      appendAttribution();
      resultsBox.hidden = false;
      input.setAttribute("aria-expanded", "true");
      announce(`${items.length} ${labels.search_results}`);
      // Fresh results replace the cached preview. Keep the place the user already highlighted.
      if (previousActive) {
        const index = items.findIndex((item) => sameResult(item, previousActive));
        if (index >= 0) setActive(index);
      }
    }

    function renderMessage(message) {
      items = [];
      resultsBox.replaceChildren();
      const error = document.createElement("div");
      error.className = "result-error";
      error.setAttribute("role", "option");
      error.setAttribute("aria-disabled", "true");
      error.textContent = message;
      resultsBox.append(error);
      appendAttribution();
      resultsBox.hidden = false;
      input.setAttribute("aria-expanded", "true");
      announce(message);
    }

    function errorMessage(body, response) {
      if (response.status === 429 || body?.code === "throttled") return labels.search_throttled;
      if (response.status === 504 || body?.code === "provider_timeout") return labels.search_timeout;
      return labels.search_error;
    }

    async function search(query, immediate = false) {
      cancelSearch();
      hideResults();
      const normalized = normalizeSearch(query);
      const language = document.documentElement.lang || "it";
      const cacheKey = `${language}|${normalized}`;
      const currentGeneration = generation;
      const minimum = 2;
      if (normalized.length < minimum) {
        announce(normalized ? labels.search_min_length : "");
        return;
      }
      const cached = searchCache.get(cacheKey);
      if (cached && cached.expires > Date.now()) {
        renderResults(cached.results);
        return;
      }
      let preview = cachedSuggestions(normalized, language);
      if (preview.length) renderResults(preview);
      const run = async () => {
        const controller = new AbortController();
        request = controller;
        if (!preview.length) announce(labels.search_loading);
        try {
          const queryString = new URLSearchParams({
            q: query.trim(),
            lang: language,
          }).toString();
          const cachedResponse = await fetch(`/api/v1/places/search?${queryString}&cached_only=1`, {
            headers: { Accept: "application/json" },
            signal: controller.signal,
          });
          const cachedBody = await cachedResponse.json().catch(() => ({}));
          if (currentGeneration !== generation || controller.signal.aborted) return;
          if (cachedResponse.ok && cachedBody.complete === true) {
            const results = Array.isArray(cachedBody.results) ? cachedBody.results : [];
            rememberSearch(cacheKey, results);
            renderResults(results);
            return;
          }
          if (cachedResponse.ok && cachedBody.results?.length) {
            preview = cachedBody.results;
            renderResults(preview);
          }
          const response = await fetch(`/api/v1/places/search?${queryString}`, {
            headers: { Accept: "application/json" },
            signal: controller.signal,
          });
          const body = await response.json().catch(() => ({}));
          if (currentGeneration !== generation || controller.signal.aborted) return;
          if (!response.ok) {
            if (preview.length) announce(errorMessage(body, response));
            else renderMessage(errorMessage(body, response));
            return;
          }
          const results = Array.isArray(body.results) ? body.results : [];
          rememberSearch(cacheKey, results);
          renderResults(results);
        } catch (error) {
          if (error.name === "AbortError" || currentGeneration !== generation) return;
          if (preview.length) announce(labels.search_error);
          else renderMessage(labels.search_error);
        } finally {
          if (currentGeneration === generation) request = null;
        }
      };
      if (immediate) {
        await run();
      } else {
        timer = window.setTimeout(run, 220);
      }
    }

    function choose(item) {
      if (!item || !Number.isFinite(Number(item.latitude)) || !Number.isFinite(Number(item.longitude))) return;
      beginSelectionIntent();
      clearStoredGeolocation();
      input.value = item.name || input.value;
      closeResults();
      announce(item.name || "");
      if (liveMapController?.selectDestination) {
        liveMapController.selectDestination({
          latitude: Number(item.latitude),
          longitude: Number(item.longitude),
          source: "search",
          name: item.name || "",
        });
      } else {
        window.__sporameteoPendingDestination = {
          latitude: Number(item.latitude),
          longitude: Number(item.longitude),
          source: "search",
          name: item.name || "",
        };
      }
    }

    function showLocationError(message) {
      announce(message);
      toast(message);
      locationButton?.removeAttribute("aria-busy");
    }

    function locationError(error, intent) {
      if (intent !== selectionGeneration) {
        locationButton?.removeAttribute("aria-busy");
        return;
      }
      const messages = {
        1: labels.location_denied_detail,
        2: labels.location_unavailable,
        3: labels.location_timeout,
      };
      showLocationError(messages[error?.code] || labels.location_unavailable);
    }

    function locate() {
      const intent = beginSelectionIntent();
      if (!window.isSecureContext && !["localhost", "127.0.0.1", "[::1]"].includes(window.location.hostname)) {
        showLocationError(labels.location_insecure);
        return;
      }
      if (!navigator.geolocation?.getCurrentPosition) {
        showLocationError(labels.location_unsupported);
        return;
      }
      locationButton?.setAttribute("aria-busy", "true");
      announce(labels.location_loading);
      navigator.geolocation.getCurrentPosition(
        (position) => {
          if (intent !== selectionGeneration) {
            locationButton?.removeAttribute("aria-busy");
            return;
          }
          locationButton?.removeAttribute("aria-busy");
          const point = {
            latitude: Number(position.coords.latitude),
            longitude: Number(position.coords.longitude),
            source: "geolocation",
          };
          if (liveMapController?.selectDestination) {
            liveMapController.selectDestination(point);
          } else {
            window.__sporameteoPendingDestination = point;
          }
          announce(labels.location);
        },
        (error) => locationError(error, intent),
        { enableHighAccuracy: false, maximumAge: 300_000, timeout: 10_000 },
      );
    }

    input.addEventListener("input", () => search(input.value));
    input.addEventListener("keydown", (event) => {
      if (event.key === "ArrowDown") {
        event.preventDefault();
        setActive(activeIndex + 1);
      } else if (event.key === "ArrowUp") {
        event.preventDefault();
        setActive(activeIndex - 1);
      } else if (event.key === "Enter") {
        event.preventDefault();
        if (items[activeIndex]) choose(items[activeIndex]);
        else search(input.value, true);
      } else if (event.key === "Escape") {
        closeResults();
      }
    });
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      if (items[activeIndex]) choose(items[activeIndex]);
      else search(input.value, true);
    });
    locationButton?.addEventListener("click", locate);
    document.addEventListener("click", (event) => {
      if (!form.contains(event.target)) closeResults();
    });
    return { search, choose, locate, closeResults };
  }

  function persistState() {
    root.classList.toggle("has-overlay", state.details || Boolean(state.panel));
    const next = new URL(window.location.href);
    const hasVerifiedRepresentative = Boolean(
      state.representative
      && state.detailRepresentativeSquare === state.square
      && state.detailRepresentativeDate === state.date
      && state.detailRepresentativeUpdate
      && String(state.detailRepresentativeUpdate) === String(state.updateId || ""),
    );
    const verifiedRepresentative = hasVerifiedRepresentative ? state.representative : null;
    const geolocationSelection = state.pointSource === "geolocation" && state.point;
    if (geolocationSelection) {
      try {
        window.sessionStorage.setItem(geolocationStateKey, JSON.stringify({
          source: "geolocation",
          point: state.point,
          representative: verifiedRepresentative,
          square: state.square,
          center: state.center,
          zoom: state.zoom,
        }));
      } catch (error) {
        // Private browsing can disable session storage. The in-memory state still works.
      }
    } else {
      clearStoredGeolocation();
    }
    const values = {
      date: state.date,
      result: state.resultId,
      assessment: state.assessment,
      update: state.updateId,
      panel: state.panel,
      details: String(state.details),
      sheet: state.sheetHeight,
      background: state.background,
      overlay: String(state.overlay),
      opacity: String(state.opacity),
      foldedLayers: String(state.foldedLayers),
      foldedLegend: String(state.foldedLegend),
      foldedDates: String(state.foldedDates),
      legendMore: String(state.legendMore),
    };
    if (!geolocationSelection) {
      Object.assign(values, {
        square: state.square,
        center: state.center,
        zoom: String(state.zoom),
        point_latitude: state.point ? String(state.point.latitude) : "",
        point_longitude: state.point ? String(state.point.longitude) : "",
        representative_latitude: verifiedRepresentative ? String(verifiedRepresentative.latitude) : "",
        representative_longitude: verifiedRepresentative ? String(verifiedRepresentative.longitude) : "",
        point_source: state.pointSource,
      });
    }
    if (geolocationSelection) locationQueryKeys.forEach((key) => next.searchParams.delete(key));
    Object.entries(values).forEach(([key, value]) => {
      if (value) next.searchParams.set(key, value);
      else next.searchParams.delete(key);
    });
    window.history.replaceState({}, "", next);
    Object.assign(root.dataset, {
      selectedSquare: state.square,
      targetDate: state.date,
      assessmentDate: state.assessment,
      updateId: state.updateId,
      resultId: state.resultId,
      mapCenter: state.center,
      mapZoom: String(state.zoom),
      pointLatitude: state.point ? String(state.point.latitude) : "",
      pointLongitude: state.point ? String(state.point.longitude) : "",
      representativeLatitude: verifiedRepresentative ? String(verifiedRepresentative.latitude) : "",
      representativeLongitude: verifiedRepresentative ? String(verifiedRepresentative.longitude) : "",
      selectionSource: state.pointSource,
      detailsOpen: String(state.details),
      panel: state.panel,
    });
    updateNavigationAction();
    window.dispatchEvent(new CustomEvent("sporameteo:selectionchange", {
      detail: {
        point: state.point,
        representative: verifiedRepresentative,
        destination: state.point || verifiedRepresentative,
        square: state.square || null,
        source: state.pointSource || null,
        mapCentre: state.center ? parseMapCenter(state.center) : null,
      },
    }));
  }

  function parseMapCenter(value) {
    const values = String(value || "").split(",").map(Number);
    if (values.length !== 2 || values.some((part) => !Number.isFinite(part))) return null;
    return { latitude: values[0], longitude: values[1] };
  }

  function setDate(button) {
    document.querySelectorAll(".date-btn").forEach((item) => {
      const selected = item === button;
      item.classList.toggle("is-selected", selected);
      item.setAttribute("aria-pressed", String(selected));
    });
    state.date = button.dataset.date || state.date;
    const displayedDate = document.querySelector(".date-big");
    if (displayedDate) displayedDate.textContent = fullDate(state.date);
    const estimateText = button.querySelector(".d-pct")?.textContent.trim() || "";
    const percentage = document.querySelector(".pct-badge .pct");
    if (percentage) {
      if (button.dataset.estimateState === "unavailable") percentage.textContent = "-";
      else if (estimateText) percentage.textContent = estimateText.endsWith("%") ? estimateText : `${estimateText}%`;
    }
    persistState();
    liveMapController?.setDate(state.date);
  }

  document.querySelectorAll(".date-btn").forEach((button) => button.addEventListener("click", () => setDate(button)));
  document.querySelectorAll("[data-zoom]").forEach((button) => button.addEventListener("click", () => {
    state.zoom = Math.min(19, Math.max(1, state.zoom + (button.dataset.zoom === "in" ? 1 : -1)));
    persistState();
    if (liveMapController) {
      liveMapController.zoom(button.dataset.zoom === "in" ? 1 : -1);
    } else {
      toast(labels.later_map);
    }
  }));

  function closePanel(name) {
    const panel = document.querySelector(`[data-fold-card="${name}"]`);
    if (!panel) return;
    panel.classList.remove("is-open");
    const trigger = document.querySelector(`[data-open-panel="${name}"]`);
    if (trigger) trigger.setAttribute("aria-expanded", "false");
    if (state.panel === name) state.panel = "";
    root.classList.remove("panel-open");
    document.querySelector("#main")?.classList.remove("panel-open");
    persistState();
    trigger?.focus();
  }

  document.querySelectorAll("[data-open-panel]").forEach((button) => button.addEventListener("click", () => {
    const name = button.dataset.openPanel;
    const panel = document.querySelector(`[data-fold-card="${name}"]`);
    if (!panel) return;
    const open = !panel.classList.contains("is-open");
    if (state.panel && state.panel !== name) closePanel(state.panel);
    // The phone sheet has no fold button, so opening it must show the folded body.
    const body = document.getElementById(`${name}-body`);
    if (open && body?.hidden) {
      body.hidden = false;
      panel.classList.remove("is-folded");
      document.querySelector(`[data-fold="${name}"]`)?.setAttribute("aria-expanded", "true");
      state[{layers: "foldedLayers", legend: "foldedLegend", dates: "foldedDates"}[name]] = false;
    }
    panel.classList.toggle("is-open", open);
    button.setAttribute("aria-expanded", String(open));
    state.panel = open ? name : "";
    root.classList.toggle("panel-open", open);
    document.querySelector("#main")?.classList.toggle("panel-open", open);
    persistState();
    if (open) panel.querySelector("button")?.focus();
  }));
  document.querySelectorAll("[data-close-panel]").forEach((button) => button.addEventListener("click", () => closePanel(button.dataset.closePanel)));

  document.querySelectorAll("[data-fold]").forEach((button) => button.addEventListener("click", () => {
    const body = document.getElementById(`${button.dataset.fold}-body`);
    const card = document.querySelector(`[data-fold-card="${button.dataset.fold}"]`);
    if (!body || !card) return;
    const folded = body.hidden = !body.hidden;
    card.classList.toggle("is-folded", folded);
    button.setAttribute("aria-expanded", String(!folded));
    state[{layers: "foldedLayers", legend: "foldedLegend", dates: "foldedDates"}[button.dataset.fold]] = folded;
    persistState();
  }));

  const more = document.querySelector("[data-legend-more]");
  if (more) more.addEventListener("click", () => {
    const extra = document.querySelector(".lg-extra");
    if (!extra) return;
    extra.hidden = !extra.hidden;
    state.legendMore = !extra.hidden;
    more.textContent = extra.hidden ? labels.legend_more : labels.legend_less;
    more.setAttribute("aria-expanded", String(state.legendMore));
    persistState();
  });

  const overlay = document.querySelector("[data-overlay]");
  function setOverlay(value) {
    state.overlay = value;
    root.classList.toggle("overlay-off", !value);
    if (overlay) {
      overlay.setAttribute("aria-checked", String(value));
      overlay.classList.toggle("is-on", value);
    }
    persistState();
    liveMapController?.setOverlay(value);
  }
  if (overlay) {
    overlay.addEventListener("click", () => setOverlay(!state.overlay));
  }
  const opacity = document.querySelector("[data-opacity]");
  if (opacity) opacity.addEventListener("input", () => {
    state.opacity = Number(opacity.value);
    const output = document.querySelector("[data-opacity-value]");
    if (output) output.value = `${state.opacity}%`;
    root.style.setProperty("--overlay-opacity", String(state.opacity / 100));
    persistState();
    liveMapController?.setOpacity(state.opacity);
  });
  document.querySelectorAll("input[name=background]").forEach((input) => input.addEventListener("change", () => {
    state.background = input.value;
    root.classList.toggle("bg-contrast", state.background === "contrast");
    persistState();
    liveMapController?.setBackground(state.background);
  }));

  function setDetails(open) {
    if (!open) liveMapController?.closeDetails();
    const details = document.querySelector(".details[data-details]");
    if (!details) return;
    state.details = open;
    root.classList.toggle("details-open", open);
    document.querySelector("#main")?.classList.toggle("details-open", open);
    details.hidden = !open;
    document.querySelectorAll("[data-center-select]").forEach((button) => button.removeAttribute("hidden"));
    persistState();
    liveMapController?.resize();
    if (!open) document.getElementById("map")?.focus();
  }
  document.querySelectorAll("[data-close-details]").forEach((button) => button.addEventListener("click", () => setDetails(false)));
  const heightButton = document.querySelector("button[data-sheet-height]");
  if (heightButton) heightButton.addEventListener("click", () => {
    const details = document.querySelector(".details[data-details]");
    if (!details) return;
    const sizes = ["short", "half", "tall"];
    state.sheetHeight = sizes[(sizes.indexOf(state.sheetHeight) + 1) % sizes.length];
    details.dataset.sheetHeight = state.sheetHeight;
    details.dataset.size = state.sheetHeight;
    persistState();
    liveMapController?.resize();
  });

  document.querySelectorAll("[data-center-select]").forEach((button) => button.addEventListener("click", () => {
    if (button.closest("aside[data-details]")) {
      setDetails(false);
      if (!liveMapController?.centerSelection()) toast(labels.later_map);
    } else if (liveMapController?.centerSelection()) {
      return;
    } else if (root.dataset.fixture && document.querySelector("aside[data-details]")) {
      setDetails(true);
      document.getElementById("details-heading")?.focus();
    } else toast(labels.later_map);
  }));
  let favoriteDialog = null;
  let favoriteTrigger = null;

  function parseFavoriteCoordinates(value) {
    const matches = String(value || "").match(/(-?\d+(?:\.\d+)?)[^\d-]+(-?\d+(?:\.\d+)?)/);
    if (!matches) return null;
    const latitude = Number(matches[1]);
    const longitude = Number(matches[2]);
    if (!Number.isFinite(latitude) || !Number.isFinite(longitude)) return null;
    return { latitude, longitude };
  }

  function currentFavoriteSelection() {
    const selected = liveMapController?.getFavoriteSelection?.();
    if (selected) return selected;
    if (root.dataset.fixture && state.square) {
      const point = parseFavoriteCoordinates(document.querySelector(".d-coord")?.textContent);
      if (point) return { kind: "grid", ...point, cellId: state.square, name: "" };
    }
    if (state.point) return { kind: "point", ...state.point, name: state.pointName };
    return null;
  }

  function favoriteName(selection) {
    if (selection.name) return selection.name;
    if (selection.kind === "grid") return labels.favorite_default_grid.replace("{cell}", selection.cellId);
    return labels.favorite_default_point
      .replace("{lat}", Number(selection.latitude).toFixed(4))
      .replace("{lon}", Number(selection.longitude).toFixed(4));
  }

  function favoriteStatus(reading) {
    if (reading.status === "warning") return labels.favorite_storage_malformed;
    if (reading.status === "unknown") return labels.favorite_storage_unknown;
    if (reading.status === "unreadable") return labels.favorite_read_error;
    if (reading.status === "unavailable") return labels.favorite_storage_unavailable;
    return "";
  }

  function favoriteReadFailure(reading) {
    if (reading && !["unknown", "unreadable", "unavailable"].includes(reading.status)) return "";
    return favoriteStatus(reading || { status: "unavailable" });
  }

  function favoriteWriteFailure(result) {
    if (result.ok) return "";
    return result.reason === "quota"
      ? labels.favorite_storage_quota
      : result.reason === "write" ? labels.favorite_storage_write : labels.favorite_storage_unavailable;
  }

  function updateFavoriteCount(items) {
    const count = document.querySelector("[data-favourite-count]");
    if (count) count.textContent = items.length ? String(items.length) : "";
  }

  function ensureFavoriteDialog() {
    if (favoriteDialog) return favoriteDialog;
    favoriteDialog = document.createElement("dialog");
    favoriteDialog.className = "favorites-dialog";
    favoriteDialog.dataset.favoritesDialog = "";
    favoriteDialog.addEventListener("close", () => {
      favoriteTrigger?.focus();
      favoriteTrigger = null;
    });
    document.body.append(favoriteDialog);
    return favoriteDialog;
  }

  function closeFavoriteDialog() {
    if (favoriteDialog?.open) favoriteDialog.close();
  }

  function renderFavoriteHeader(title, showBack = false) {
    const header = document.createElement("div");
    header.className = "favorites-header";
    const heading = document.createElement("h2");
    heading.id = "favorites-dialog-heading";
    heading.textContent = title;
    header.append(heading);
    ensureFavoriteDialog().setAttribute("aria-labelledby", heading.id);
    const close = document.createElement("button");
    close.type = "button";
    close.className = "btn btn-secondary";
    close.dataset.favoriteClose = "";
    close.textContent = showBack ? labels.favorite_cancel : labels.favorite_close;
    close.addEventListener("click", closeFavoriteDialog);
    header.append(close);
    return header;
  }

  function renderFavoriteList(reading) {
    const dialog = ensureFavoriteDialog();
    const items = reading.items;
    updateFavoriteCount(items);
    dialog.replaceChildren(renderFavoriteHeader(labels.favorites));
    const note = document.createElement("p");
    note.className = "favorites-note";
    note.textContent = labels.favorite_storage_note;
    dialog.append(note);
    const warning = favoriteStatus(reading);
    if (warning) {
      const status = document.createElement("p");
      status.className = "favorites-status";
      status.setAttribute("role", "status");
      status.textContent = warning;
      dialog.append(status);
    }
    const list = document.createElement("div");
    list.className = "favorites-list";
    list.setAttribute("aria-live", "polite");
    if (!items.length) {
      const empty = document.createElement("p");
      empty.className = "favorites-empty";
      empty.textContent = labels.favorite_empty;
      list.append(empty);
    }
    items.forEach((favorite) => {
      const card = document.createElement("article");
      card.className = "favorite-card";
      card.dataset.favoriteId = favorite.id;
      if (favorite.color) card.dataset.favoriteColor = favorite.color;
      const title = document.createElement("h3");
      title.textContent = favorite.name;
      card.append(title);
      const kind = document.createElement("p");
      kind.className = "favorite-kind";
      kind.textContent = favorite.kind === "grid" ? labels.favorite_grid : labels.favorite_point;
      card.append(kind);
      const coordinate = document.createElement("p");
      coordinate.className = "favorite-coordinate";
      coordinate.textContent = favorite.kind === "grid"
        ? favorite.cellId
        : `${favorite.latitude.toFixed(4)}° N, ${favorite.longitude.toFixed(4)}° E`;
      card.append(coordinate);
      const result = document.createElement("p");
      result.className = "favorite-result";
      result.textContent = labels.favorite_open_for_result;
      card.append(result);
      const actions = document.createElement("div");
      actions.className = "favorite-actions";
      const open = document.createElement("button");
      open.type = "button";
      open.className = "btn btn-primary";
      open.dataset.favoriteOpen = favorite.id;
      open.textContent = labels.favorite_open;
      open.addEventListener("click", () => {
        closeFavoriteDialog();
        liveMapController?.openFavorite?.(favorite);
      });
      const edit = document.createElement("button");
      edit.type = "button";
      edit.className = "btn btn-secondary";
      edit.dataset.favoriteEdit = favorite.id;
      edit.textContent = labels.favorite_edit;
      edit.addEventListener("click", () => openFavoriteEditor(favorite));
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "btn btn-quiet";
      remove.dataset.favoriteRemove = favorite.id;
      remove.textContent = labels.favorite_remove;
      remove.addEventListener("click", () => removeFavorite(favorite.id));
      actions.append(open, edit, remove);
      card.append(actions);
      list.append(card);
    });
    dialog.append(list);
    const save = document.createElement("button");
    save.type = "button";
    save.className = "btn btn-primary favorites-save-selected";
    save.dataset.favoriteSaveSelected = "";
    save.textContent = labels.favorite_save_selected;
    save.addEventListener("click", () => openFavoriteEditor());
    dialog.append(save);
    if (dialog.open) dialog.querySelector("[data-favorite-close]")?.focus();
  }

  function openFavoriteEditor(existing = null) {
    const selection = existing || currentFavoriteSelection();
    if (!selection && !existing) {
      toast(labels.not_selected);
      return;
    }
    const dialog = ensureFavoriteDialog();
    dialog.replaceChildren(renderFavoriteHeader(existing ? labels.favorite_edit : labels.favorite_save_selected, true));
    const form = document.createElement("form");
    form.className = "favorite-form";
    const nameLabel = document.createElement("label");
    nameLabel.textContent = labels.favorite_name;
    const name = document.createElement("input");
    name.type = "text";
    name.required = true;
    name.maxLength = 160;
    name.dataset.favoriteName = "";
    name.value = existing?.name || favoriteName(selection);
    nameLabel.append(name);
    form.append(nameLabel);
    const colorLabel = document.createElement("label");
    colorLabel.textContent = labels.favorite_color;
    const color = document.createElement("select");
    color.dataset.favoriteColor = "";
    const none = document.createElement("option");
    none.value = "";
    none.textContent = labels.favorite_color_none;
    color.append(none);
    (window.SporaMeteoFavorites?.colors || []).forEach((value) => {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = labels[`favorite_color_${value}`] || value;
      color.append(option);
    });
    color.value = existing?.color || "";
    colorLabel.append(color);
    form.append(colorLabel);
    const actions = document.createElement("div");
    actions.className = "favorite-form-actions";
    const cancel = document.createElement("button");
    cancel.type = "button";
    cancel.className = "btn btn-secondary";
    cancel.textContent = labels.favorite_cancel;
    cancel.addEventListener("click", () => renderFavoriteList(window.SporaMeteoFavorites.read()));
    const submit = document.createElement("button");
    submit.type = "submit";
    submit.className = "btn btn-primary";
    submit.textContent = labels.favorite_save;
    actions.append(cancel, submit);
    form.append(actions);
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      saveFavorite({ selection, existing, name: name.value, color: color.value || undefined });
    });
    dialog.append(form);
    if (!dialog.open) dialog.showModal();
    name.focus();
  }

  function favoriteFeedback(message) {
    if (!favoriteDialog) return;
    let status = favoriteDialog.querySelector("[data-favorite-form-status]");
    if (!status) {
      status = document.createElement("p");
      status.className = "favorites-status";
      status.dataset.favoriteFormStatus = "";
      status.setAttribute("role", "alert");
      const target = favoriteDialog.querySelector(".favorite-form") || favoriteDialog;
      target.prepend(status);
    }
    status.textContent = message;
  }

  function saveFavorite({ selection, existing, name, color }) {
    const reading = window.SporaMeteoFavorites?.read?.();
    const readMessage = favoriteReadFailure(reading);
    if (readMessage) {
      favoriteFeedback(readMessage);
      toast(readMessage);
      return;
    }
    const candidate = window.SporaMeteoFavorites.create({
      ...(existing || selection),
      id: existing?.id,
      name,
      color: color || undefined,
    });
    if (!candidate) {
      toast(labels.favorite_invalid);
      favoriteFeedback(labels.favorite_invalid);
      return;
    }
    const duplicate = reading.items.find((item) => item.id !== candidate.id && window.SporaMeteoFavorites.sameDestination(item, candidate));
    if (duplicate) {
      openFavoriteEditor(duplicate);
      toast(labels.favorite_duplicate);
      return;
    }
    const items = existing
      ? reading.items.map((item) => item.id === existing.id ? candidate : item)
      : [...reading.items, candidate];
    const result = window.SporaMeteoFavorites.write(items, reading.invalid);
    const writeMessage = favoriteWriteFailure(result);
    if (writeMessage) {
      favoriteFeedback(writeMessage);
      toast(writeMessage);
      return;
    }
    updateFavoriteCount(items);
    toast(existing ? labels.favorite_updated : labels.favorite_saved);
    renderFavoriteList({
      status: reading.invalid.length ? "warning" : "ready",
      items,
      invalid: reading.invalid,
    });
  }

  function removeFavorite(id) {
    const reading = window.SporaMeteoFavorites?.read?.();
    const readMessage = favoriteReadFailure(reading);
    if (readMessage) {
      favoriteFeedback(readMessage);
      toast(readMessage);
      return;
    }
    const items = reading.items.filter((item) => item.id !== id);
    const result = window.SporaMeteoFavorites.write(items, reading.invalid);
    const writeMessage = favoriteWriteFailure(result);
    if (writeMessage) {
      favoriteFeedback(writeMessage);
      toast(writeMessage);
      return;
    }
    toast(labels.favorite_removed);
    renderFavoriteList({
      status: reading.invalid.length ? "warning" : "ready",
      items,
      invalid: reading.invalid,
    });
  }

  function openFavoritesDialog(trigger) {
    favoriteTrigger = trigger;
    const dialog = ensureFavoriteDialog();
    renderFavoriteList(window.SporaMeteoFavorites.read());
    if (!dialog.open) dialog.showModal();
    dialog.querySelector("[data-favorite-close]")?.focus();
  }

  document.querySelector("[data-favourites]")?.addEventListener("click", (event) => openFavoritesDialog(event.currentTarget));
  document.querySelectorAll("[data-save]").forEach((button) => button.addEventListener("click", (event) => {
    favoriteTrigger = event.currentTarget;
    openFavoriteEditor();
  }));
  document.querySelectorAll("[data-share-link]").forEach((button) => button.addEventListener("click", copyPageLink));

  async function copyPageLink() {
    try {
      await navigator.clipboard.writeText(window.location.href);
      toast(labels.link_copied);
    } catch {
      // The clipboard API needs a secure page and can be blocked by the browser.
      toast(labels.link_copy_failed);
    }
  }
  updateFavoriteCount(window.SporaMeteoFavorites?.read?.().items || []);
  window.addEventListener("storage", (event) => {
    if (event.key !== window.SporaMeteoFavorites?.key || !favoriteDialog?.open) return;
    renderFavoriteList(window.SporaMeteoFavorites.read());
  });
  document.querySelectorAll("[data-try-again]").forEach((button) => button.addEventListener("click", () => window.location.reload()));

  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    if (favoriteDialog?.open) return;
    if (state.panel) closePanel(state.panel);
    else if (state.details) setDetails(false);
  });

  const selectedDate = document.querySelector(`.date-btn[data-date="${CSS.escape(state.date)}"]`);
  if (selectedDate) {
    setDate(selectedDate);
    selectedDate.scrollIntoView({ block: "nearest", inline: "center" });
  }
  document.querySelectorAll("input[name=background]").forEach((input) => { input.checked = input.value === state.background; });
  root.classList.toggle("bg-contrast", state.background === "contrast");
  setOverlay(state.overlay);
  if (opacity) { opacity.value = String(state.opacity); opacity.dispatchEvent(new Event("input")); }
  const details = document.querySelector(".details[data-details]");
  if (details) {
    details.dataset.sheetHeight = state.sheetHeight;
    details.dataset.size = state.sheetHeight;
    details.hidden = !state.details;
    root.classList.toggle("details-open", state.details);
    document.querySelector("#main")?.classList.toggle("details-open", state.details);
  }
  for (const name of ["layers", "legend", "dates"]) {
    const folded = state[{layers: "foldedLayers", legend: "foldedLegend", dates: "foldedDates"}[name]];
    const body = document.getElementById(`${name}-body`);
    if (body) body.hidden = folded;
    document.querySelector(`[data-fold-card="${name}"]`)?.classList.toggle("is-folded", folded);
    document.querySelector(`[data-fold="${name}"]`)?.setAttribute("aria-expanded", String(!folded));
  }
  const extra = document.querySelector(".lg-extra");
  if (extra) extra.hidden = !state.legendMore;
  if (more) {
    more.textContent = state.legendMore ? labels.legend_less : labels.legend_more;
    more.setAttribute("aria-expanded", String(state.legendMore));
  }
  const panel = document.querySelector(`[data-fold-card="${state.panel}"]`);
  if (panel) {
    panel.classList.add("is-open");
    root.classList.add("panel-open");
    document.querySelector("#main")?.classList.add("panel-open");
    document.querySelector(`[data-open-panel="${state.panel}"]`)?.setAttribute("aria-expanded", "true");
  }
  persistState();

  function createLiveMapController() {
    if (root.dataset.fixture) return null;
    const container = document.querySelector("[data-map-container]");
    if (!container) return null;

    const EMPTY = { type: "FeatureCollection", features: [] };
    const styles = {
      light: "https://tiles.openfreemap.org/styles/liberty",
      contrast: "https://tiles.openfreemap.org/styles/positron",
    };
    const stateData = {
      info: null,
      forest: null,
      squares: null,
      selected: null,
      request: null,
      forestRequest: null,
      forestVisible: false,
      squaresRendered: false,
      pendingSelectionId: null,
      pendingSelectionUpdateId: null,
      pendingSelectionDetails: false,
      pendingSelectionPoint: null,
      pendingSelectionRepresentative: null,
      pendingSelectionSource: "",
      pendingFavoriteCell: null,
      pendingDestination: null,
      destinationRequest: null,
      destinationGeneration: 0,
      debounce: null,
      generation: 0,
      requestCount: 0,
      squareSetCount: 0,
      displayedDate: null,
      displayedRunId: null,
      runRecheckId: null,
      renderCount: 0,
      styleLoads: 0,
      ready: false,
      lastRequest: null,
      listenerCount: 0,
      dateTimer: null,
      lastView: null,
      movementInvalidated: false,
      moveFrame: null,
      moveStarts: 0,
      moveEnds: 0,
      invalidationCount: 0,
      resizeObserver: null,
      resizeWidth: 0,
      resizeHeight: 0,
      estimateRequest: null,
      estimateGeneration: 0,
      estimateCache: new Map(),
      estimateFeature: null,
      detailRequest: null,
      detailGeneration: 0,
      detailData: null,
      detailRainDays: 7,
      styleRequest: null,
      styleGeneration: 0,
      styleRestoreGeneration: -1,
      styleMessageId: null,
      initialising: false,
      backgroundKey: state.background,
    };

    function clearPendingSelection() {
      stateData.pendingSelectionId = null;
      stateData.pendingSelectionUpdateId = null;
      stateData.pendingSelectionDetails = false;
      stateData.pendingSelectionPoint = null;
      stateData.pendingSelectionRepresentative = null;
      stateData.pendingSelectionSource = "";
      stateData.pendingFavoriteCell = null;
    }

    const squareLayers = [
      "sporameteo-square-estimates",
      "sporameteo-square-missing",
      "sporameteo-square-noforest",
      "sporameteo-noforest-line",
      "sporameteo-grid-lines",
      "sporameteo-missing-line",
      "sporameteo-selected",
      "sporameteo-weather-area",
    ];

    function listen(event, listener, layer) {
      stateData.listenerCount += 1;
      if (layer) stateData.map.on(event, layer, listener);
      else stateData.map.on(event, listener);
    }

    function listenOnce(event, listener) {
      stateData.listenerCount += 1;
      stateData.map.once(event, listener);
    }

    let messageIdentity = 0;
    function message(text, kind = "wait", retry = false, retryAction = null, source = "map") {
      const identity = String(++messageIdentity);
      const target = document.querySelector("#mapmsgs");
      if (!target) return identity;
      target.replaceChildren();
      if (!text) return identity;
      const item = document.createElement("div");
      item.className = `msg msg-${kind}`;
      item.dataset.liveState = kind;
      item.dataset.liveMessageId = identity;
      item.dataset.liveMessageSource = source;
      const copy = document.createElement("span");
      copy.textContent = text;
      item.append(copy);
      if (retry) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "btn btn-secondary";
        button.textContent = labels.map_retry || labels.try_again;
        if (retryAction) button.addEventListener("click", retryAction);
        item.append(button);
      }
      target.append(item);
      return identity;
    }

    function currentMessage() {
      const item = document.querySelector("#mapmsgs > [data-live-message-id]");
      if (!item) return null;
      return {
        id: item.dataset.liveMessageId,
        source: item.dataset.liveMessageSource,
      };
    }

    if (!window.maplibregl) {
      message(labels.map_library_error, "error", true, () => window.location.reload());
      return null;
    }

    function parseCenter(value) {
      const values = String(value || "").split(",").map(Number);
      if (values.length !== 2 || values.some((part) => !Number.isFinite(part))) return null;
      const [lat, lon] = values;
      if (lat < -90 || lat > 90 || lon < -180 || lon > 180) return null;
      return [lon, lat];
    }

    function emptySource(name) {
      return stateData.map?.getSource(name)?.setData(EMPTY);
    }

    function setSquareLayersVisible(visible, record = true) {
      const map = stateData.map;
      if (!map) return;
      if (record) stateData.squaresRendered = visible;
      const value = visible && state.overlay ? "visible" : "none";
      squareLayers.forEach((id) => {
        if (map.getLayer(id)) map.setLayoutProperty(id, "visibility", value);
      });
    }

    function setForestVisible(visible, clearSource = false) {
      const map = stateData.map;
      const wasVisible = stateData.forestVisible;
      stateData.forestVisible = visible;
      if (!map) return;
      if (map.getLayer("sporameteo-forest")) {
        map.setLayoutProperty("sporameteo-forest", "visibility", visible && state.overlay ? "visible" : "none");
      }
      if (clearSource && wasVisible) emptySource("forest");
    }

    function invalidateAreaDetails() {
      stateData.detailGeneration += 1;
      stateData.detailRequest?.abort();
      stateData.detailRequest = null;
      stateData.detailData = null;
    }

    function clearVisibleSelection() {
      invalidateAreaDetails();
      stateData.weatherLabel?.remove();
      stateData.weatherLabel = null;
      const hadSelection = Boolean(stateData.selected);
      stateData.selected = null;
      if (hadSelection) {
        emptySource("selected-square");
        emptySource("weather-area");
      }
      document.querySelector("aside[data-live-details]")?.remove();
      if (!root.dataset.fixture) document.querySelector("aside[data-details]:not([data-live-details])")?.remove();
      const percentage = document.querySelector(".pct-badge .pct");
      if (percentage) percentage.textContent = "-";
      const unit = document.querySelector(".pct-badge .pct-unit");
      if (unit) unit.textContent = labels.estimate_unavailable;
    }

    function clearSquares() {
      const hadSquares = Boolean(stateData.squares);
      stateData.displayedDate = null;
      stateData.displayedRunId = null;
      const keepSelection = state.details && stateData.selected?.properties?.target_date === state.date;
      if (!keepSelection) {
        updateDateMetadata([], true);
        stateData.estimateGeneration += 1;
        stateData.estimateRequest?.abort();
        stateData.estimateRequest = null;
        stateData.estimateFeature = null;
        clearDateEstimates();
      }
      if (state.square && !keepSelection) {
        stateData.pendingSelectionId = state.square;
        stateData.pendingSelectionUpdateId = state.updateId;
        stateData.pendingSelectionDetails = state.details;
        stateData.pendingSelectionPoint = state.point;
        stateData.pendingSelectionRepresentative = ["map", "centre"].includes(state.pointSource)
          ? null
          : state.representative;
        stateData.pendingSelectionSource = state.pointSource;
        state.square = "";
        state.updateId = "";
        state.details = false;
        root.classList.remove("details-open");
        document.querySelector("#main")?.classList.remove("details-open");
        persistState();
      }
      stateData.squares = null;
      setSquareLayersVisible(false);
      if (!keepSelection) clearVisibleSelection();
      if (hadSquares) emptySource("squares");
      updateStale();
      stateData.renderCount += 1;
    }

    function updateStale() {
      const stale = Boolean(stateData.squares) && stateData.displayedDate !== state.date;
      if (stale) root.dataset.mapStale = "true";
      else delete root.dataset.mapStale;
      applyOpacity(state.opacity);
    }

    function revealSquaresWhenRendered() {
      const map = stateData.map;
      if (!map) return;
      const reveal = () => {
        if (!stateData.squares) return;
        setSquareLayersVisible(true);
      };
      map.once("idle", reveal);
      map.triggerRepaint();
    }

    function renderDates(targetDates) {
      const buttons = [...document.querySelectorAll(".date-btn")];
      targetDates.forEach((value, index) => {
        const button = buttons[index];
        if (!button) return;
        const current = new Date(`${value}T12:00:00Z`);
        button.dataset.date = value;
        button.querySelector(".d-wd").textContent = value === stateData.info?.today ? labels.today : new Intl.DateTimeFormat(locale(), { weekday: "short", timeZone: "Europe/Rome" }).format(current);
        button.querySelector(".d-dm").textContent = new Intl.DateTimeFormat(locale(), { day: "numeric", month: "short", timeZone: "Europe/Rome" }).format(current);
        button.querySelector(".d-pct").textContent = "";
      });
      clearDateEstimates();
      buttons.slice(targetDates.length).forEach((button) => button.closest("li")?.remove());
      const selected = document.querySelector(`.date-btn[data-date="${CSS.escape(state.date)}"]`) || buttons[0];
      if (selected) setDate(selected);
    }

    function clearDateEstimates() {
      document.querySelectorAll(".date-btn").forEach((button) => {
        const estimate = button.querySelector(".d-pct");
        if (estimate) estimate.textContent = "";
        delete button.dataset.estimateState;
      });
    }

    function setDateEstimate(targetDate, value, unavailable = false) {
      const button = document.querySelector(`.date-btn[data-date="${CSS.escape(targetDate)}"]`);
      if (!button) return;
      const estimate = button.querySelector(".d-pct");
      if (!estimate) return;
      estimate.textContent = unavailable ? (labels.map_date_unavailable || "-") : `${value}%`;
      button.dataset.estimateState = unavailable ? "unavailable" : "available";
    }

    function clearDateEstimate(targetDate) {
      const button = document.querySelector(`.date-btn[data-date="${CSS.escape(targetDate)}"]`);
      if (!button) return;
      const estimate = button.querySelector(".d-pct");
      if (estimate) estimate.textContent = "";
      delete button.dataset.estimateState;
    }

    function loadSelectedEstimates(feature) {
      const properties = feature?.properties || {};
      const targetDates = stateData.info?.target_dates || [];
      const assessmentDate = properties.assessment_date;
      const updateId = properties.update_id;
      const cellId = properties.cell_id || feature?.id;
      stateData.estimateFeature = feature;
      stateData.estimateGeneration += 1;
      stateData.estimateRequest?.abort();
      stateData.estimateRequest = null;
      clearDateEstimates();
      if (!targetDates.length || !assessmentDate || !updateId || !cellId) {
        targetDates.forEach((targetDate) => setDateEstimate(targetDate, null, true));
        return;
      }
      const generation = stateData.estimateGeneration;
      const assessmentTime = new Date(`${assessmentDate}T12:00:00Z`).getTime();
      const eligibleDates = targetDates.filter((targetDate) => {
        const targetTime = new Date(`${targetDate}T12:00:00Z`).getTime();
        return targetTime >= assessmentTime && targetTime <= assessmentTime + 7 * 86400000;
      });
      targetDates.filter((targetDate) => !eligibleDates.includes(targetDate)).forEach((targetDate) => setDateEstimate(targetDate, null, true));
      const cacheKey = `${stateData.info?.run?.run_id || ""}|${cellId}|${updateId}`;
      let cached = stateData.estimateCache.get(cacheKey);
      if (!cached) {
        cached = new Map();
        stateData.estimateCache.set(cacheKey, cached);
        while (stateData.estimateCache.size > 12) stateData.estimateCache.delete(stateData.estimateCache.keys().next().value);
      }
      const missingDates = eligibleDates.filter((targetDate) => !cached.has(targetDate));
      eligibleDates.filter((targetDate) => cached.has(targetDate)).forEach((targetDate) => {
        const result = cached.get(targetDate);
        setDateEstimate(targetDate, result.value, result.unavailable);
      });
      if (!missingDates.length) return;
      const controller = new AbortController();
      stateData.estimateRequest = controller;
      Promise.all(missingDates.map(async (targetDate) => {
        const query = new URLSearchParams({ target_date: targetDate, update_id: updateId });
        try {
          const { response, body } = await fetchJson(`/api/v1/areas/${encodeURIComponent(cellId)}?${query}`, { signal: controller.signal });
          if (!response.ok) return [targetDate, { error: true }];
          if (body?.estimate?.whole_percentage === null || body?.estimate?.whole_percentage === undefined) {
            return [targetDate, { value: null, unavailable: true }];
          }
          const value = Number(body.estimate.whole_percentage);
          return Number.isFinite(value)
            ? [targetDate, { value, unavailable: false }]
            : [targetDate, { error: true }];
        } catch (error) {
          if (error.name === "AbortError") throw error;
          return [targetDate, { error: true }];
        }
      })).then((entries) => {
        if (generation !== stateData.estimateGeneration) return;
        let failed = false;
        entries.forEach(([targetDate, result]) => {
          if (result.error) {
            failed = true;
            clearDateEstimate(targetDate);
            return;
          }
          cached.set(targetDate, result);
          setDateEstimate(targetDate, result.value, result.unavailable);
        });
        if (failed) {
          message(labels.map_request_error, "error", true, () => loadSelectedEstimates(feature));
        }
      }).catch((error) => {
        if (error.name !== "AbortError" && generation === stateData.estimateGeneration) {
          missingDates.forEach((targetDate) => clearDateEstimate(targetDate));
          message(labels.map_request_error, "error", true, () => loadSelectedEstimates(feature));
        }
      }).finally(() => {
        if (generation === stateData.estimateGeneration) stateData.estimateRequest = null;
      });
    }

    function applyBasePalette() {
      const map = stateData.map;
      if (!map) return;
      const contrast = state.background === "contrast";
      const background = contrast ? "#e0e0de" : "#f5f0e6";
      const layers = map.getStyle()?.layers || [];
      layers.forEach((layer) => {
        if (layer.id.startsWith("sporameteo-")) return;
        if (layer.type === "background") {
          map.setPaintProperty(layer.id, "background-color", background);
        }
        if (layer.type !== "fill") return;
        const id = layer.id.toLowerCase();
        let fillColor = contrast ? "#d0d0ce" : "#ebe5d9";
        if (/water|river|lake|ocean|sea/.test(id)) {
          fillColor = contrast ? "#c3c7c7" : "#dbe4e1";
        } else if (/building|built/.test(id)) {
          fillColor = contrast ? "#d9d9d7" : "#eeeae2";
        }
        if (/land|park|wood|forest|grass|water|river|lake|ocean|sea|building|built/.test(id)) {
          map.setPaintProperty(layer.id, "fill-color", fillColor);
          map.setPaintProperty(layer.id, "fill-opacity", contrast ? 0.92 : 1);
        }
      });
    }

    function addMissingPattern() {
      const map = stateData.map;
      if (!map || map.hasImage("sporameteo-missing")) return;
      const canvas = document.createElement("canvas");
      canvas.width = 12;
      canvas.height = 12;
      const context = canvas.getContext("2d");
      if (!context) return;
      context.fillStyle = "#d8d8d4";
      context.fillRect(0, 0, 12, 12);
      context.strokeStyle = "#888984";
      context.lineWidth = 2;
      context.beginPath();
      context.moveTo(-2, 10);
      context.lineTo(10, -2);
      context.moveTo(2, 14);
      context.lineTo(14, 2);
      context.stroke();
      map.addImage("sporameteo-missing", context.getImageData(0, 0, 12, 12), { pixelRatio: 1 });
    }

    function addSourcesAndLayers() {
      const map = stateData.map;
      if (!map) return;
      stateData.styleLoads += 1;
      applyBasePalette();
      addMissingPattern();
      if (!map.getSource("forest")) map.addSource("forest", { type: "geojson", data: EMPTY });
      if (!map.getSource("squares")) map.addSource("squares", { type: "geojson", data: EMPTY, promoteId: "cell_id" });
      if (!map.getSource("selected-square")) map.addSource("selected-square", { type: "geojson", data: EMPTY });
      if (!map.getSource("weather-area")) map.addSource("weather-area", { type: "geojson", data: EMPTY });
      if (!map.getLayer("sporameteo-forest")) {
        map.addLayer({
          id: "sporameteo-forest",
          type: "fill",
          source: "forest",
          paint: {
            "fill-color": ["interpolate", ["linear"], ["coalesce", ["get", "forest_share"], 0], 0, "rgba(60,110,89,0)", 0.1, "rgba(60,110,89,0.1)", 1, "rgba(60,110,89,0.58)"],
            "fill-opacity": 0.72,
          },
        });
      }
      if (!map.getLayer("sporameteo-square-estimates")) {
        map.addLayer({
          id: "sporameteo-square-estimates",
          type: "fill",
          source: "squares",
          filter: ["==", ["get", "state"], "available"],
          paint: {
            "fill-color": ["interpolate", ["linear"], ["coalesce", ["get", "percentage"], 0], ...estimateColors],
            "fill-opacity": 0.6,
            "fill-antialias": false,
          },
        });
      }
      if (!map.getLayer("sporameteo-square-missing")) {
        map.addLayer({
          id: "sporameteo-square-missing",
          type: "fill",
          source: "squares",
          filter: ["in", ["get", "state"], ["literal", ["unavailable", "noresultyet", "expired"]]],
          paint: { "fill-color": "#d8d8d4", "fill-pattern": "sporameteo-missing", "fill-opacity": 0.72 },
        });
      }
      if (!map.getLayer("sporameteo-square-noforest")) {
        map.addLayer({
          id: "sporameteo-square-noforest",
          type: "fill",
          source: "squares",
          filter: ["==", ["get", "state"], "noforest"],
          paint: { "fill-color": "rgba(0,0,0,0)", "fill-opacity": 0 },
        });
      }
      if (!map.getLayer("sporameteo-noforest-line")) {
        map.addLayer({
          id: "sporameteo-noforest-line",
          type: "line",
          source: "squares",
          filter: ["==", ["get", "state"], "noforest"],
          paint: { "line-color": "#777872", "line-width": 1.25, "line-dasharray": [1, 2], "line-opacity": 0.9 },
        });
      }
      if (!map.getLayer("sporameteo-grid-lines")) {
        map.addLayer({
          id: "sporameteo-grid-lines",
          type: "line",
          source: "squares",
          minzoom: 12,
          filter: ["==", ["get", "state"], "available"],
          paint: { "line-color": "rgba(17,34,27,0.25)", "line-width": 0.5, "line-opacity": 0.7 },
        });
      }
      if (!map.getLayer("sporameteo-missing-line")) {
        map.addLayer({
          id: "sporameteo-missing-line",
          type: "line",
          source: "squares",
          filter: ["in", ["get", "state"], ["literal", ["unavailable", "noresultyet", "expired"]]],
          paint: { "line-color": "#777872", "line-width": 1, "line-dasharray": [2, 2], "line-opacity": 0.9 },
        });
      }
      if (!map.getLayer("sporameteo-weather-area")) {
        map.addLayer({
          id: "sporameteo-weather-area",
          type: "line",
          source: "weather-area",
          paint: { "line-color": "#2e5645", "line-width": 2, "line-dasharray": [2, 2], "line-opacity": 0.85 },
        });
      }
      if (!map.getLayer("sporameteo-selected")) {
        map.addLayer({
          id: "sporameteo-selected",
          type: "line",
          source: "selected-square",
          paint: { "line-color": "#2e5645", "line-width": 3, "line-gap-width": 2 },
        });
      }
      if (stateData.forest && stateData.forestVisible) map.getSource("forest")?.setData(stateData.forest);
      if (stateData.squares) map.getSource("squares")?.setData(stateData.squares);
      if (stateData.selected) drawSelection(stateData.selected);
      setForestVisible(stateData.forestVisible);
      setSquareLayersVisible(false);
      if (stateData.squares) revealSquaresWhenRendered();
      applyOverlay(state.overlay);
      applyOpacity(state.opacity);
    }

    function restoreCustomLayers() {
      const map = stateData.map;
      if (!map || !map.isStyleLoaded?.()) return;
      if (map.getSource("squares") && map.getLayer("sporameteo-square-estimates")) return;
      if (stateData.styleRestoreGeneration === stateData.styleGeneration) return;
      stateData.styleRestoreGeneration = stateData.styleGeneration;
      addSourcesAndLayers();
    }

    function applyOverlay(enabled) {
      const map = stateData.map;
      if (!map) return;
      setSquareLayersVisible(stateData.squaresRendered, false);
      if (map.getLayer("sporameteo-forest")) {
        map.setLayoutProperty("sporameteo-forest", "visibility", enabled && stateData.forestVisible ? "visible" : "none");
      }
    }

    function applyOpacity(value) {
      const map = stateData.map;
      if (!map) return;
      // Estimates of another date stay on screen, dimmed, until the new date arrives.
      const dimming = root.dataset.mapStale === "true" ? 0.4 : 1;
      const opacity = Math.max(0, Math.min(1, Number(value) / 100)) * dimming;
      if (map.getLayer("sporameteo-square-estimates")) map.setPaintProperty("sporameteo-square-estimates", "fill-opacity", opacity);
      if (map.getLayer("sporameteo-square-missing")) map.setPaintProperty("sporameteo-square-missing", "fill-opacity", opacity);
      if (map.getLayer("sporameteo-square-noforest")) map.setPaintProperty("sporameteo-square-noforest", "fill-opacity", 0);
      if (map.getLayer("sporameteo-noforest-line")) map.setPaintProperty("sporameteo-noforest-line", "line-opacity", opacity);
      if (map.getLayer("sporameteo-missing-line")) map.setPaintProperty("sporameteo-missing-line", "line-opacity", opacity);
    }

    function featureCenter(feature) {
      const coordinates = feature.geometry?.coordinates?.[0] || [];
      const points = coordinates.flatMap((ring) => Array.isArray(ring?.[0]) ? ring : [ring]);
      const valid = points.filter((point) => Array.isArray(point) && point.length >= 2);
      if (!valid.length) return null;
      const longitudes = valid.map((point) => point[0]);
      const latitudes = valid.map((point) => point[1]);
      return [
        (Math.min(...longitudes) + Math.max(...longitudes)) / 2,
        (Math.min(...latitudes) + Math.max(...latitudes)) / 2,
      ];
    }

    function areaFeature(feature) {
      const properties = feature.properties || {};
      const cell = properties.weather_cell;
      if (!cell || properties.state === "noforest" || properties.reason === "forest_missing" || properties.unavailable_reason === "forest_missing") return EMPTY;
      const lat = Number(cell.latitude);
      const lon = Number(cell.longitude);
      if (!Number.isFinite(lat) || !Number.isFinite(lon) || lat < -90 || lat > 90 || lon < -180 || lon > 180) return EMPTY;
      const lngHalf = 0.05;
      const latHalf = 0.05;
      return {
        type: "FeatureCollection",
        features: [{ type: "Feature", geometry: { type: "Polygon", coordinates: [[[lon - lngHalf, lat - latHalf], [lon + lngHalf, lat - latHalf], [lon + lngHalf, lat + latHalf], [lon - lngHalf, lat + latHalf], [lon - lngHalf, lat - latHalf]]] }, properties: {} }],
      };
    }

    function plainFeature(feature) {
      if (!feature) return null;
      try {
        return JSON.parse(JSON.stringify({
          type: "Feature",
          id: feature.id,
          geometry: feature.geometry,
          properties: feature.properties ?? {},
        }));
      } catch (error) {
        return null;
      }
    }

    function drawSelection(feature) {
      const map = stateData.map;
      if (!map) return;
      const selection = { type: "FeatureCollection", features: [feature] };
      map.getSource("selected-square")?.setData(selection);
      const weatherArea = areaFeature(feature);
      map.getSource("weather-area")?.setData(weatherArea);
      stateData.weatherLabel?.remove();
      stateData.weatherLabel = null;
      const weatherCell = feature.properties?.weather_cell;
      if (weatherArea.features.length) {
        const label = document.createElement("span");
        label.className = "live-weather-label";
        label.textContent = `${labels.weather_area}: ~11 × 8 km`;
        stateData.weatherLabel = new window.maplibregl.Marker({ element: label, anchor: "bottom-left" })
          .setLngLat([Number(weatherCell.longitude) - 0.05, Number(weatherCell.latitude) + 0.05])
          .addTo(map);
      }
    }

    function createLiveDetails() {
      let details = document.querySelector("aside[data-live-details]");
      if (details) return details;
      const wrapper = document.querySelector("#sheetwrap");
      if (!wrapper) return null;
      details = document.createElement("aside");
      details.className = "details live-details";
      details.dataset.liveDetails = "true";
      details.dataset.details = "true";
      details.dataset.sheetHeight = state.sheetHeight;
      details.dataset.size = state.sheetHeight;
      details.setAttribute("aria-label", labels.details_label);
      wrapper.append(details);
      return details;
    }

    function appendDetailText(parent, className, text) {
      if (!text) return;
      const item = document.createElement("p");
      if (className) item.className = className;
      item.textContent = text;
      parent.append(item);
    }

    // The short phone panel hides the text and keeps the icon, so the aria-label keeps the name.
    function setIconLabel(button, pathData, text) {
      const icon = document.createElementNS("http://www.w3.org/2000/svg", "svg");
      icon.setAttribute("class", "icon");
      icon.setAttribute("viewBox", "0 0 24 24");
      icon.setAttribute("aria-hidden", "true");
      const shape = document.createElementNS("http://www.w3.org/2000/svg", "path");
      shape.setAttribute("d", pathData);
      icon.append(shape);
      const label = document.createElement("span");
      label.className = "btn-text";
      label.textContent = text;
      button.setAttribute("aria-label", text);
      button.replaceChildren(icon, label);
    }

    function appendFavoriteAction(details) {
      const actions = document.createElement("div");
      actions.className = "d-actions";
      const save = document.createElement("button");
      save.type = "button";
      save.className = "btn btn-secondary favorite-icon";
      save.dataset.save = "";
      save.setAttribute("aria-label", labels.save_favorite);
      save.title = labels.save_favorite;
      const icon = document.createElementNS("http://www.w3.org/2000/svg", "svg");
      icon.setAttribute("class", "icon");
      icon.setAttribute("viewBox", "0 0 24 24");
      icon.setAttribute("aria-hidden", "true");
      const star = document.createElementNS("http://www.w3.org/2000/svg", "path");
      star.setAttribute("d", "M12 3l2.8 5.7 6.2.9-4.5 4.4 1.1 6.2L12 17.2 6.4 20.2l1.1-6.2L3 9.6l6.2-.9z");
      icon.append(star);
      save.append(icon);
      save.addEventListener("click", (event) => {
        favoriteTrigger = event.currentTarget;
        openFavoriteEditor();
      });
      const share = document.createElement("button");
      share.type = "button";
      share.className = "btn btn-secondary favorite-icon";
      share.dataset.shareLink = "";
      share.setAttribute("aria-label", labels.copy_link);
      share.title = labels.copy_link;
      const shareIcon = document.createElementNS("http://www.w3.org/2000/svg", "svg");
      shareIcon.setAttribute("class", "icon");
      shareIcon.setAttribute("viewBox", "0 0 24 24");
      shareIcon.setAttribute("aria-hidden", "true");
      const shareShape = document.createElementNS("http://www.w3.org/2000/svg", "path");
      shareShape.setAttribute("d", "M18 8a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM6 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM18 22a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM8.6 13.5l6.8 4M15.4 6.5l-6.8 4");
      shareIcon.append(shareShape);
      share.append(shareIcon);
      share.addEventListener("click", copyPageLink);
      const navigation = document.createElement("button");
      navigation.type = "button";
      navigation.className = "btn btn-primary";
      navigation.dataset.navigation = "";
      setIconLabel(navigation, "M7 17L17 7M8 7h9v9", labels.open_navigation);
      navigation.disabled = true;
      navigation.addEventListener("click", () => {
        const url = buildGoogleMapsUrl(navigationCoordinates());
        if (!url) return;
        window.open(url, "_blank", "noopener,noreferrer");
      });
      actions.append(save, share, navigation);
      const status = document.createElement("p");
      status.className = "navigation-status";
      status.dataset.navigationStatus = "";
      status.setAttribute("role", "status");
      status.setAttribute("aria-live", "polite");
      status.hidden = true;
      details.append(actions, status);
      updateNavigationAction();
    }

    function appendUniqueDetailLink(parent, className, url, text, seen) {
      if (!url || !/^https?:\/\//.test(url) || !text || seen.has(url)) return;
      seen.add(url);
      const link = document.createElement("a");
      if (className) link.className = className;
      link.href = url;
      link.rel = "noreferrer";
      link.textContent = text;
      parent.append(link);
    }

    function factLabel(fact) {
      const labelsByRole = {
        recent_rain: labels.fact_recent_rain,
        older_rain: labels.fact_older_rain,
        forest_share: labels.fact_forest_share,
        temperature: labels.fact_temperature,
        soil_moisture: labels.fact_soil_moisture,
        drying: labels.fact_drying,
        frost: labels.fact_frost,
        target_temperature_at_reference: labels.fact_target_temperature,
        mean_temperature_at_reference: labels.fact_mean_temperature,
        corrected_temperature: labels.fact_corrected_temperature,
        corrected_mean_temperature: labels.fact_corrected_mean_temperature,
        reference_altitude: labels.fact_reference_altitude,
        square_altitude: labels.fact_square_altitude,
        frost_days: labels.fact_frost_days,
        host_tree_factor: labels.fact_host_tree_factor,
        host_broadleaf: labels.fact_host_broadleaf,
        host_conifer: labels.fact_host_conifer,
        host_mixed: labels.fact_host_mixed,
        host_beech: labels.fact_host_beech,
        host_oak: labels.fact_host_oak,
        host_chestnut: labels.fact_host_chestnut,
        host_oaks: labels.fact_host_oaks,
        host_pines: labels.fact_host_pines,
        host_spruce_fir: labels.fact_host_spruce_fir,
        rain_30_days: labels.fact_rain_30_days,
        retained_water: labels.fact_retained_water,
      };
      if (labelsByRole[fact.role_key]) return labelsByRole[fact.role_key];
      return labels.fact_input;
    }

    function factMeasurement(value, unit, roleKey) {
      if (roleKey === "forest_share" || unit === "fraction" || unit === "relative_occurrence") {
        return measurement(Number(value) <= 1 ? Number(value) * 100 : Number(value), "%");
      }
      const units = {
        deg_c: "°C",
        mm: "mm",
        m: "m",
        multiplier: "x",
      };
      return measurement(value, units[unit] || unit || "");
    }

    function formatCoordinates(coordinates) {
      if (!coordinates) return "";
      const latitude = Number(coordinates.latitude);
      const longitude = Number(coordinates.longitude);
      if (!Number.isFinite(latitude) || !Number.isFinite(longitude)) return "";
      const latDirection = latitude < 0 ? "S" : "N";
      const lonDirection = longitude < 0 ? "W" : "E";
      return `${Math.abs(latitude).toFixed(4)}° ${latDirection}, ${Math.abs(longitude).toFixed(4)}° ${lonDirection}`;
    }

    function classificationText(properties, detail) {
      const propertyCode = properties?.reason || properties?.unavailable_reason;
      const code = detail?.code === "estimate_not_available"
        ? propertyCode || detail.code
        : detail?.code || propertyCode;
      return {
        no_forest: labels.detail_no_forest,
        outside_prepared_area: labels.detail_outside_prepared_area,
        noforest: labels.detail_no_forest,
        forest_below_minimum: labels.detail_forest_below_minimum,
        missing_forest_data: labels.detail_required_missing,
        estimate_not_available: labels.detail_required_missing,
        older_rain: labels.detail_older_rain,
        recent_rain: labels.detail_recent_rain,
        target_temperature: labels.detail_target_temperature,
        reference_altitude: labels.detail_reference_altitude,
        square_altitude: labels.detail_square_altitude,
        weather_summary_missing: labels.detail_weather_summary_missing,
        terrain_data_missing: labels.detail_terrain_data_missing,
        period_temperature: labels.detail_period_temperature,
        weather_history: labels.detail_weather_history,
        missing_rain_mm: labels.detail_missing_rain_mm,
        missing_temperature_c: labels.detail_missing_temperature_c,
        missing_et0_mm: labels.detail_missing_et0_mm,
        requiredgap: labels.detail_required_gap,
        pending: labels.detail_pending,
        noresultyet: labels.detail_pending,
        visitor_limit: labels.detail_visitor_limit,
        quota_exhausted: labels.detail_quota_exhausted,
        provider_unavailable: labels.detail_provider_unavailable,
        provider_refused: labels.detail_provider_refused,
        target_outside_update_range: labels.detail_target_outside_update_range,
        update_expired: labels.detail_update_expired,
        square_unknown: labels.detail_no_detail,
        update_square_mismatch: labels.detail_no_detail,
      }[code] || labels.detail_required_missing;
    }

    function statusText(detail) {
      const estimate = detail?.estimate;
      if (estimate?.state === "available" && estimate.whole_percentage !== null && estimate.whole_percentage !== undefined) {
        return `${estimate.whole_percentage}%`;
      }
      if (estimate?.state === "noforest") return labels.detail_no_forest;
      if (estimate?.state === "noresultyet") return labels.detail_pending;
      if (estimate?.state === "expired") return labels.map_expired;
      const reason = estimate?.unavailable_reason;
      return classificationText({}, { code: reason });
    }

    function addStatusCallout(details, text, kind = "info", retryAction = null, actionLabel = labels.detail_retry) {
      const callout = document.createElement("div");
      callout.className = `callout callout-${kind}`;
      callout.setAttribute("role", kind === "error" ? "alert" : "status");
      appendDetailText(callout, "", text);
      if (retryAction) {
        const retry = document.createElement("button");
        retry.type = "button";
        retry.className = "btn btn-secondary";
        retry.textContent = actionLabel;
        retry.addEventListener("click", retryAction);
        callout.append(retry);
      }
      details.append(callout);
    }

    function appendExperimentalNotice(details) {
      const notice = document.createElement("div");
      notice.className = "notice-experimental";
      notice.setAttribute("data-detail-experimental", "true");
      const paragraph = document.createElement("p");
      const title = document.createElement("strong");
      title.textContent = labels.experimental_label;
      paragraph.append(title, ` ${labels.experimental_body}`);
      notice.append(paragraph);
      details.append(notice);
    }

    function createDetailDisclosure(key, summaryText, className = "") {
      const disclosure = document.createElement("details");
      disclosure.className = `detail-disclosure${className ? ` ${className}` : ""}`;
      disclosure.dataset.detailDisclosure = key;
      const summary = document.createElement("summary");
      summary.textContent = summaryText;
      disclosure.append(summary);
      return disclosure;
    }

    function appendWeather(details, weather) {
      const section = document.createElement("section");
      section.className = "d-sec";
      const heading = document.createElement("h3");
      heading.textContent = labels.weather_heading;
      section.append(heading);
      const selected = weather?.selected_day;
      const tileGrid = document.createElement("div");
      tileGrid.className = "tiles detail-weather";
      section.append(tileGrid);
      if (selected?.state === "available") {
        const values = [
          [labels.detail_weather_date, selected.date ? date(selected.date) : ""],
          [labels.temperature, selected.mean_temperature_c === null || selected.mean_temperature_c === undefined ? "" : measurement(selected.mean_temperature_c, "°C")],
          [labels.detail_weather_rain, selected.rain_mm === null || selected.rain_mm === undefined ? "" : measurement(selected.rain_mm, "mm")],
          [labels.detail_weather_minimum, selected.min_temperature_c === null || selected.min_temperature_c === undefined ? "" : measurement(selected.min_temperature_c, "°C")],
        ];
        values.forEach(([label, value]) => {
          if (!value) return;
          const tile = document.createElement("div");
          tile.className = "tile";
          appendDetailText(tile, "tile-label", label);
          appendDetailText(tile, "tile-text", value);
          tileGrid.append(tile);
        });
        const selectedCounts = document.createElement("p");
        selectedCounts.className = "detail-weather-counts";
        const completed = selected.completed_day_count;
        const forecast = selected.forecast_day_count;
        const countParts = [];
        if (completed !== null && completed !== undefined) {
          countParts.push(labels.detail_completed_days.replace("{count}", String(completed)));
        }
        if (forecast !== null && forecast !== undefined) {
          countParts.push(labels.detail_forecast_days.replace("{count}", String(forecast)));
        }
        selectedCounts.textContent = countParts.join(". ");
        if (countParts.length) section.append(selectedCounts);

        const extraValues = [];
        if (selected.et0_mm !== null && selected.et0_mm !== undefined) extraValues.push([labels.detail_weather_et0, measurement(selected.et0_mm, "mm")]);
        if (selected.mean_soil_temperature_c !== null && selected.mean_soil_temperature_c !== undefined) extraValues.push([labels.detail_weather_soil_temperature, measurement(selected.mean_soil_temperature_c, "°C")]);
        if (selected.mean_soil_moisture !== null && selected.mean_soil_moisture !== undefined) extraValues.push([labels.detail_weather_soil_moisture, measurement(selected.mean_soil_moisture, "m³/m³")]);
        if (selected.frost !== null && selected.frost !== undefined) extraValues.push([labels.frost, selected.frost ? labels.detail_frost_yes : labels.detail_frost_no]);
        if (extraValues.length) {
          const disclosure = createDetailDisclosure("weather-extra", labels.detail_weather_more, "detail-weather-more");
          const extraGrid = document.createElement("div");
          extraGrid.className = "tiles detail-weather-extra";
          extraValues.forEach(([label, value]) => {
            const tile = document.createElement("div");
            tile.className = "tile";
            appendDetailText(tile, "tile-label", label);
            appendDetailText(tile, "tile-text", value);
            extraGrid.append(tile);
          });
          disclosure.append(extraGrid);
          section.append(disclosure);
        }
      } else {
        addStatusCallout(section, labels.detail_weather_missing, "info");
        if (selected?.missing_reason_key) {
          appendDetailText(
            section,
            "detail-weather-reason",
            labels.detail_missing_reason.replace("{reason}", weatherGapReason(selected.missing_reason_key)),
          );
        }
      }
      const gaps = Array.isArray(weather?.variable_gaps) ? weather.variable_gaps : [];
      if (gaps.length) {
        const disclosure = createDetailDisclosure("weather-gaps", labels.detail_weather_gaps, "detail-weather-more");
        const gapList = document.createElement("ul");
        gapList.className = "detail-gaps";
        gaps.forEach((gap) => {
          const item = document.createElement("li");
          const variable = {
            et0: labels.detail_weather_et0,
            frost: labels.frost,
            rain: labels.detail_weather_rain,
            precipitation: labels.detail_weather_precipitation,
            temperature: labels.temperature,
            temperature_c: labels.temperature,
            min_temperature_c: labels.detail_weather_minimum,
            soil_temperature: labels.detail_weather_soil_temperature,
            soil_temperature_c: labels.detail_weather_soil_temperature,
            soil_moisture: labels.detail_weather_soil_moisture,
          }[gap.variable] || labels.detail_weather_variable;
          const dateText = gap.date ? date(gap.date) : labels.detail_missing_value;
          const unitText = gap.unit ? ` ${weatherUnit(gap.unit)}` : "";
          const hours = [];
          if (gap.expected_hours !== null && gap.expected_hours !== undefined) {
            hours.push(labels.detail_expected_hours.replace("{count}", String(gap.expected_hours)));
          }
          if (gap.present_hours !== null && gap.present_hours !== undefined) {
            hours.push(labels.detail_present_hours.replace("{count}", String(gap.present_hours)));
          }
          item.textContent = `${dateText}: ${variable}${unitText}. ${weatherGapReason(gap.reason_key)}${hours.length ? ` ${hours.join(". ")}.` : ""}`;
          gapList.append(item);
        });
        disclosure.append(gapList);
        section.append(disclosure);
      }
      details.append(section);
    }

    function weatherGapReason(reason) {
      return {
        optional_variable_absent: labels.detail_gap_optional,
        required_variable_absent: labels.detail_gap_required,
        missing_hour: labels.detail_gap_missing_hours,
        missing_hours: labels.detail_gap_missing_hours,
        incomplete_day: labels.detail_gap_missing_day,
        weather_selected_day_missing: labels.detail_gap_selected_weather,
        weather_recent_rain_missing: labels.detail_gap_recent_rain,
        "weather.recent_rain_missing": labels.detail_gap_recent_rain,
        "weather.selected_day_missing": labels.detail_gap_selected_weather,
      }[reason] || labels.detail_gap_unknown;
    }

    function weatherUnit(unit) {
      return {
        deg_c: "°C",
        m3_m3: "m³/m³",
        mm: "mm",
        m: "m",
      }[unit] || unit;
    }

    function appendRain(details, weather) {
      const section = document.createElement("section");
      section.className = "d-sec";
      const heading = document.createElement("h3");
      heading.textContent = labels.rain_heading;
      section.append(heading);
      const controls = document.createElement("div");
      controls.className = "rain-periods";
      [7, 14, 21].forEach((days) => {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "btn btn-secondary";
        button.dataset.rainDays = String(days);
        button.setAttribute("aria-pressed", String(stateData.detailRainDays === days));
        button.textContent = `${days} ${labels.days}`;
        button.addEventListener("click", () => {
          button.focus();
          stateData.detailRainDays = days;
          if (stateData.detailData) renderDetailContent(stateData.detailData, stateData.detailFeature);
        });
        controls.append(button);
      });
      section.append(controls);
      const key = `recent_rain_${stateData.detailRainDays}_days`;
      const period = weather?.[key];
      const row = document.createElement("div");
      row.className = "rain-row detail-rain-value";
      const title = document.createElement("span");
      title.textContent = period?.period_start && period?.period_end
        ? `${date(period.period_start)} - ${date(period.period_end)}`
        : labels.detail_period_missing;
      const value = document.createElement("strong");
      value.textContent = period?.state === "available" && period.value !== null && period.value !== undefined
        ? measurement(period.value, period.unit || "mm")
        : labels.detail_missing_value;
      row.append(title, value);
      section.append(row);
      const periodCounts = [];
      if (period?.completed_day_count !== null && period?.completed_day_count !== undefined) {
        periodCounts.push(labels.detail_completed_days.replace("{count}", String(period.completed_day_count)));
      }
      if (period?.forecast_day_count !== null && period?.forecast_day_count !== undefined) {
        periodCounts.push(labels.detail_forecast_days.replace("{count}", String(period.forecast_day_count)));
      }
      if (periodCounts.length) appendDetailText(section, "rain-counts", periodCounts.join(". "));
      if (period?.missing_reason_key) {
        appendDetailText(
          section,
          "rain-missing-reason",
          labels.detail_missing_reason.replace("{reason}", weatherGapReason(period.missing_reason_key)),
        );
      }
      const note = labels.rain_note.replace("{until}", period?.period_end ? date(period.period_end) : labels.detail_missing_value);
      appendDetailText(section, "rain-note", note);
      details.append(section);
    }

    function appendSources(details, detail) {
      const section = createDetailDisclosure("sources", labels.sources_heading, "d-sec sources");
      const sourceBody = document.createElement("div");
      sourceBody.className = "source-list";
      section.append(sourceBody);
      const sources = detail?.sources || {};
      const knownFactSources = new Set();
      const seenUrls = new Set();
      const sourceNames = {
        grid: labels.detail_source_grid,
        forest: labels.detail_source_forest,
        terrain: labels.detail_source_terrain,
        weather: labels.detail_source_weather,
      };
      const linkLabels = {
        catalogue: labels.detail_source_catalogue,
        service: labels.detail_source_service,
        page: labels.detail_source_page,
        credit: labels.detail_source_credit,
      };
      const cardValues = new WeakMap();
      const longSourceBodies = new WeakMap();

      function sourceName(key, source) {
        // Provider dataset names are in English. They stay visible on the version line.
        if (sourceNames[key]) return sourceNames[key];
        const candidate = source?.name || source?.provider || "";
        return candidate && !/^\s*[\d._-]+\s*$/.test(String(candidate))
          ? String(candidate)
          : sourceNames[key] || labels.detail_source_page;
      }

      function sourceField(card, className, label, value) {
        if (value === null || value === undefined || value === "") return;
        const rendered = String(value).trim();
        if (!rendered) return;
        const values = cardValues.get(card) || new Set();
        const dedupeKey = rendered.toLocaleLowerCase();
        if (values.has(dedupeKey)) return;
        values.add(dedupeKey);
        cardValues.set(card, values);
        const parent = rendered.length > 120 ? sourceLongBody(card) : card;
        appendDetailText(parent, className, `${label}: ${rendered}`);
      }

      function sourceLongBody(card) {
        const existing = longSourceBodies.get(card);
        if (existing) return existing;
        const disclosure = createDetailDisclosure(`source-provenance-${card.dataset.sourceKey}`, labels.detail_source_more, "source-more");
        const body = document.createElement("div");
        body.className = "source-more-body";
        disclosure.append(body);
        card.append(disclosure);
        longSourceBodies.set(card, body);
        return body;
      }

      function sourceLink(card, url, kind) {
        const label = linkLabels[kind] || labels.detail_source_page;
        appendUniqueDetailLink(card, "source-link", url, label, seenUrls);
      }

      ["grid", "forest", "terrain", "weather"].forEach((key) => {
        const source = sources[key];
        if (!source) return;
        (source.source_ids || []).forEach((sourceId) => knownFactSources.add(sourceId));
        (source.ids || []).forEach((sourceId) => knownFactSources.add(sourceId));
        const card = document.createElement("div");
        card.className = "source-card";
        card.dataset.sourceKey = key;
        const name = sourceName(key, source);
        appendDetailText(card, "source-name", name);
        if (source.release && String(source.release).trim() !== name) {
          sourceField(card, "source-release", labels.detail_release, source.release);
        }
        if (source.native_resolution_m) sourceField(card, "source-meta", labels.detail_resolution, measurement(source.native_resolution_m, "m"));
        if (source.retrieval_time) sourceField(card, "source-meta", labels.detail_retrieved, dateTime(source.retrieval_time));
        const creditText = typeof source.credit === "string" ? source.credit : source.credit?.text;
        const creditLicence = typeof source.credit === "object" ? source.credit?.licence : "";
        if (creditText) sourceField(card, "source-meta", labels.detail_credit, creditText);
        const licence = source.licence || creditLicence;
        if (licence) sourceField(card, "source-meta", labels.detail_licence, licence);
        if (source.requested_coordinates) sourceField(card, "source-meta", labels.detail_requested, formatCoordinates(source.requested_coordinates));
        if (source.returned_coordinates) sourceField(card, "source-meta", labels.detail_returned, formatCoordinates(source.returned_coordinates));
        const metadataLabels = {
          survey_year: labels.detail_survey_year,
          survey_age_years: labels.detail_survey_age,
          source_release: labels.detail_release,
          source_resolution_m: labels.detail_resolution,
          native_resolution_m: labels.detail_resolution,
        };
        Object.entries(source.source_metadata || {}).forEach(([name, value]) => {
          if (value === null || value === undefined || typeof value === "object") return;
          if (!metadataLabels[name]) return;
          const rendered = name.endsWith("_m") ? measurement(value, "m") : String(value);
          sourceField(card, "source-meta", metadataLabels[name], rendered);
        });
        (source.source_tiles || []).forEach((tile) => {
          const tileName = tile.tile_name || labels.detail_tile;
          sourceField(card, "source-meta", tileName, tile.release || tile.status || labels.detail_missing_value);
          if (tile.retrieved_at) sourceField(card, "source-meta", labels.detail_retrieved, dateTime(tile.retrieved_at));
          if (tile.credit) sourceField(card, "source-meta", labels.detail_credit, tile.credit);
          if (tile.error_detail) sourceField(card, "source-gap", labels.detail_gap, tile.error_detail);
          if (tile.url) sourceLink(card, tile.url, "page");
        });
        const gaps = [...(source.gaps || []), ...(source.variable_gaps || [])];
        gaps.forEach((gap) => {
          const reason = {
            optional_variable_absent: labels.detail_gap_optional,
            "terrain.data_missing": labels.detail_gap_terrain,
            "terrain.source_gap": labels.detail_gap_terrain,
          }[gap.reason_key] || gap.detail || gap.reason_key || labels.detail_missing_value;
          sourceField(card, "source-gap", labels.detail_gap, reason);
        });
        sourceLink(card, source.catalogue_url, "catalogue");
        sourceLink(card, source.service_url, "service");
        sourceLink(card, source.url, "page");
        sourceLink(card, source.endpoint, "service");
        sourceLink(card, source.credit?.url, "credit");
        sourceBody.append(card);
      });
      const hostFacts = (Array.isArray(detail?.facts) ? detail.facts : []).filter((fact) => {
        const roleKey = fact.role_key || fact.role || "";
        const sourceId = fact.source_id;
        return roleKey.startsWith("host_")
          && sourceId
          && !knownFactSources.has(sourceId)
          && fact.source_metadata
          && Object.keys(fact.source_metadata).length;
      });
      const hostSources = new Map();
      hostFacts.forEach((fact) => {
        const sourceId = fact.source_id;
        const quality = fact.quality || "";
        const groupKey = `${sourceId}|${quality}`;
        if (!hostSources.has(groupKey)) hostSources.set(groupKey, []);
        hostSources.get(groupKey).push(fact);
      });
      hostSources.forEach((facts, groupKey) => {
        const card = document.createElement("div");
        card.className = "source-card source-host-card";
        card.dataset.sourceKey = `host-${groupKey}`;
        const metadata = facts.find((fact) => fact.source_metadata)?.source_metadata || {};
        const quality = facts.map((fact) => fact.quality).find(Boolean);
        const qualityText = {
          modelled_1km_not_tree_cover: labels.detail_quality_modelled_host,
          forest_only_missing_host_data: labels.detail_quality_missing_host,
        }[quality];
        const hasProvenance = metadata.credit || metadata.licence || metadata.resolution_m || metadata.source_url || qualityText;
        if (!hasProvenance) return;
        appendDetailText(card, "source-name", labels.detail_source_host);
        if (metadata.credit) sourceField(card, "source-meta", labels.detail_credit, metadata.credit);
        if (metadata.licence) sourceField(card, "source-meta", labels.detail_licence, metadata.licence);
        if (metadata.resolution_m) sourceField(card, "source-meta", labels.detail_resolution, measurement(metadata.resolution_m, "m"));
        const periods = facts
          .flatMap((fact) => [fact.period_start || fact.period?.start, fact.period_end || fact.period?.end])
          .filter(Boolean)
          .sort();
        if (periods.length >= 2) sourceField(card, "source-meta", labels.detail_period, `${date(periods[0])} - ${date(periods[periods.length - 1])}`);
        if (qualityText) appendDetailText(card, "source-gap", qualityText);
        sourceLink(card, metadata.source_url, "page");
        sourceBody.append(card);
      });
      const limitationKeys = Array.isArray(sources.limitation_keys) ? sources.limitation_keys : [];
      if (limitationKeys.includes("weather.shared_across_several_kilometres")) {
        appendDetailText(sourceBody, "callout callout-info", labels.detail_weather_limit);
      }
      details.append(section);
    }

    function factIsHost(fact) {
      const roleKey = fact?.role_key || fact?.role || "";
      return roleKey.startsWith("host_");
    }

    function factActualText(fact) {
      const value = fact.actual_value ?? fact.value;
      return value === null || value === undefined
        ? labels.detail_missing_value
        : factMeasurement(value, fact.unit, fact.role_key);
    }

    function factPeriodText(fact) {
      const start = fact.period_start || fact.period?.start;
      const end = fact.period_end || fact.period?.end;
      if (!start || !end) return "";
      return start === end ? date(start) : `${date(start)} - ${date(end)}`;
    }

    function factQualityText(fact) {
      return {
        modelled_1km_not_tree_cover: labels.detail_quality_modelled_host,
        forest_only_missing_host_data: labels.detail_quality_missing_host,
        experimental_project_weight: labels.detail_quality_experimental,
      }[fact.quality] || "";
    }

    function appendFactRow(list, fact, includePeriod = false) {
      const item = document.createElement("li");
      item.className = "fact-row";
      const label = document.createElement("span");
      label.className = "fact-label";
      label.textContent = factLabel(fact);
      const value = document.createElement("strong");
      value.className = "fact-value";
      value.textContent = factActualText(fact);
      item.append(label, value);
      if (includePeriod) {
        const period = factPeriodText(fact);
        if (period) appendDetailText(item, "fact-period", period);
      }
      list.append(item);
    }

    function appendFacts(details, facts) {
      const keyFacts = facts.filter((fact) => !factIsHost(fact)).slice(0, 4);
      if (keyFacts.length) {
        const section = document.createElement("section");
        section.className = "d-sec d-sec-key-facts";
        const heading = document.createElement("h3");
        heading.textContent = labels.detail_key_facts;
        section.append(heading);
        const list = document.createElement("ul");
        list.className = "facts detail-key-facts";
        keyFacts.forEach((fact) => appendFactRow(list, fact));
        section.append(list);
        details.append(section);
      }
      if (!facts.length) return;
      const disclosure = createDetailDisclosure("facts", labels.detail_facts_more, "d-sec detail-facts");
      const list = document.createElement("ul");
      list.className = "facts facts-more";
      facts.forEach((fact) => appendFactRow(list, fact, true));
      disclosure.append(list);
      const qualities = new Set();
      facts.forEach((fact) => {
        const quality = factQualityText(fact);
        if (!quality || qualities.has(quality)) return;
        qualities.add(quality);
        appendDetailText(disclosure, "fact-quality", quality);
      });
      details.append(disclosure);
    }

    function renderDetailContent(detail, feature) {
      const details = createLiveDetails();
      if (!details) return;
      const focusedRainDays = document.activeElement?.dataset?.rainDays || "";
      const scrollTop = details.scrollTop;
      const openDisclosures = new Set(
        [...details.querySelectorAll("details[data-detail-disclosure][open]")]
          .map((disclosure) => disclosure.dataset.detailDisclosure),
      );
      details.replaceChildren();
      const selection = detail?.selection || {};
      const properties = feature?.properties || {};
      const top = document.createElement("div");
      top.className = "d-top";
      const titleGroup = document.createElement("div");
      titleGroup.className = "live-details-title";
      const heading = document.createElement("h2");
      // The square code means nothing to visitors, so name the municipality when the API sends one.
      heading.textContent = typeof selection.municipality_name === "string" && selection.municipality_name.trim()
        ? labels.map_selected_place.replace("{place}", selection.municipality_name.trim())
        : labels.map_selected;
      heading.dataset.squareId = selection.square_id || properties.cell_id || feature?.id || "";
      heading.id = "details-heading";
      heading.tabIndex = -1;
      const coordinate = document.createElement("p");
      coordinate.className = "d-coord";
      coordinate.textContent = formatCoordinates(selection.representative_coordinates) || formatCoordinates(state.representative);
      titleGroup.append(heading, coordinate);
      top.append(titleGroup);
      const close = document.createElement("button");
      close.type = "button";
      close.className = "btn btn-secondary live-details-close";
      close.textContent = "×";
      close.setAttribute("aria-label", labels.close_details);
      close.addEventListener("click", () => clearSelection());
      appendSheetHeightButtons(top);
      top.append(close);
      details.append(top);
      appendFavoriteAction(details);

      const summary = document.createElement("div");
      summary.className = "live-summary";
      const stateName = detail?.estimate?.state || properties.state || "unavailable";
      const percentage = detail?.estimate?.whole_percentage;
      const summaryText = stateName === "available" && percentage !== null && percentage !== undefined
        ? `${percentage}%`
        : statusText(detail);
      const strong = document.createElement("strong");
      strong.textContent = summaryText;
      summary.append(strong);
      appendDetailText(summary, "detail-identity", `${labels.map_target}: ${selection.target_date ? date(selection.target_date) : date(state.date)}`);
      appendDetailText(summary, "detail-identity", `${labels.assessment_date_label}: ${selection.assessment_date ? date(selection.assessment_date) : labels.detail_missing_value}`);
      appendDetailText(summary, "detail-identity", `${labels.updated}: ${detail?.estimate?.calculated_at ? dateTime(detail.estimate.calculated_at) : labels.detail_missing_value}`);
      details.append(summary);

      if (stateName === "available") appendExperimentalNotice(details);
      if (selection.assessment_date && stateData.info?.today && selection.assessment_date < stateData.info.today) {
        addStatusCallout(details, labels.detail_old_result.replace("{date}", date(selection.assessment_date)), "older");
      }
      if (stateName !== "available") addStatusCallout(details, statusText(detail), "info");

      const facts = Array.isArray(detail?.facts) ? detail.facts : [];
      appendFacts(details, facts);
      appendWeather(details, detail?.weather);
      appendRain(details, detail?.weather);
      appendSources(details, detail);
      const sizeRow = document.createElement("div");
      sizeRow.className = "sizerow";
      const closeButton = document.createElement("button");
      closeButton.type = "button";
      closeButton.className = "btn btn-quiet";
      closeButton.textContent = labels.back_to_map;
      closeButton.addEventListener("click", () => clearSelection());
      sizeRow.append(closeButton);
      details.append(sizeRow);
      state.details = true;
      root.classList.add("details-open");
      document.querySelector("#main")?.classList.add("details-open");
      details.hidden = false;
      if (selection.representative_coordinates) {
        state.representative = selection.representative_coordinates;
        persistState();
        root.dataset.representativeLatitude = String(selection.representative_coordinates.latitude);
        root.dataset.representativeLongitude = String(selection.representative_coordinates.longitude);
      }
      liveMapController?.resize();
      openDisclosures.forEach((key) => {
        const disclosure = details.querySelector(`details[data-detail-disclosure="${CSS.escape(key)}"]`);
        if (disclosure) disclosure.open = true;
      });
      details.scrollTop = scrollTop;
      if (focusedRainDays) details.querySelector(`[data-rain-days="${CSS.escape(focusedRainDays)}"]`)?.focus();
    }

    // The panel header stays in view while the panel scrolls, so the height buttons sit there.
    function appendSheetHeightButtons(top) {
      const taller = document.createElement("button");
      taller.type = "button";
      taller.className = "btn btn-secondary";
      taller.dataset.sheetHeight = "true";
      setIconLabel(taller, "M6 15l6-6 6 6", labels.sheet_taller);
      taller.addEventListener("click", () => stepSheetHeight(1));
      const smaller = document.createElement("button");
      smaller.type = "button";
      smaller.className = "btn btn-secondary";
      smaller.dataset.sheetSmaller = "true";
      setIconLabel(smaller, "M6 9l6 6 6-6", labels.sheet_smaller);
      smaller.addEventListener("click", () => stepSheetHeight(-1));
      top.append(taller, smaller);
    }

    function stepSheetHeight(step) {
      const details = document.querySelector("aside[data-live-details]");
      if (!details) return;
      const sizes = ["short", "half", "tall"];
      const index = Math.min(Math.max(sizes.indexOf(state.sheetHeight) + step, 0), sizes.length - 1);
      state.sheetHeight = sizes[index];
      details.dataset.sheetHeight = state.sheetHeight;
      details.dataset.size = state.sheetHeight;
      persistState();
      liveMapController?.resize();
      // The pressed button hides at the smallest and tallest size, so focus moves to the other one.
      if (index === 0) details.querySelector("[data-sheet-height]")?.focus();
      if (index === sizes.length - 1) details.querySelector("[data-sheet-smaller]")?.focus();
    }

    function renderDetailError(feature, retryAction) {
      renderLoadingDetails(
        feature,
        `${labels.detail_error} ${labels.detail_error_help}`,
        "error",
        retryAction,
      );
    }

    function refreshSelectedArea() {
      invalidateAreaDetails();
      state.updateId = "";
      stateData.pendingSelectionUpdateId = null;
      stateData.pendingSelectionRepresentative = null;
      state.detailRepresentative = null;
      state.detailRepresentativeSquare = "";
      state.detailRepresentativeDate = "";
      state.detailRepresentativeUpdate = "";
      persistState();
      scheduleFetch(0);
    }

    function renderLoadingDetails(
      feature,
      text = labels.detail_loading,
      kind = "wait",
      retryAction = null,
      actionLabel = labels.detail_retry,
    ) {
      const details = createLiveDetails();
      if (!details) return;
      details.replaceChildren();
      const top = document.createElement("div");
      top.className = "d-top";
      const title = document.createElement("h2");
      title.textContent = feature?.properties?.cell_id || feature?.id || labels.map_selected;
      title.id = "details-heading";
      title.tabIndex = -1;
      top.append(title);
      const close = document.createElement("button");
      close.type = "button";
      close.className = "btn btn-secondary live-details-close";
      close.textContent = "×";
      close.setAttribute("aria-label", labels.close_details);
      close.addEventListener("click", () => clearSelection());
      appendSheetHeightButtons(top);
      top.append(close);
      details.append(top);
      appendFavoriteAction(details);
      const properties = feature?.properties || {};
      const summary = document.createElement("div");
      summary.className = "live-summary";
      const strong = document.createElement("strong");
      strong.textContent = properties.state === "available" && properties.percentage !== null && properties.percentage !== undefined
        && (!properties.target_date || properties.target_date === state.date)
        ? `${properties.percentage}%`
        : kind === "wait" ? labels.detail_loading : text;
      summary.append(strong);
      appendDetailText(summary, "detail-identity", `${labels.map_target}: ${properties.target_date ? date(properties.target_date) : date(state.date)}`);
      appendDetailText(summary, "detail-identity", `${labels.assessment_date_label}: ${properties.assessment_date ? date(properties.assessment_date) : labels.detail_missing_value}`);
      appendDetailText(summary, "detail-identity", `${labels.updated}: ${properties.calculated_at ? dateTime(properties.calculated_at) : labels.detail_missing_value}`);
      details.append(summary);
      addStatusCallout(details, text, kind, retryAction, actionLabel);
      details.hidden = false;
      state.details = true;
      root.classList.add("details-open");
      document.querySelector("#main")?.classList.add("details-open");
      liveMapController?.resize();
    }

    function renderFavoriteMissing(cellId) {
      const details = createLiveDetails();
      if (!details) return;
      details.replaceChildren();
      const top = document.createElement("div");
      top.className = "d-top";
      const title = document.createElement("h2");
      title.textContent = cellId || labels.map_selected;
      title.id = "details-heading";
      title.tabIndex = -1;
      top.append(title);
      const close = document.createElement("button");
      close.type = "button";
      close.className = "btn btn-secondary live-details-close";
      close.textContent = "×";
      close.setAttribute("aria-label", labels.close_details);
      close.addEventListener("click", () => clearSelection());
      appendSheetHeightButtons(top);
      top.append(close);
      details.append(top);
      appendFavoriteAction(details);
      const summary = document.createElement("div");
      summary.className = "live-summary";
      const coordinate = state.point ? `${state.point.latitude}° N, ${state.point.longitude}° E` : "";
      const strong = document.createElement("strong");
      strong.textContent = labels.favorite_missing;
      summary.append(strong);
      appendDetailText(summary, "detail-identity", coordinate);
      details.append(summary);
      const sizeRow = document.createElement("div");
      sizeRow.className = "sizerow";
      const closeButton = document.createElement("button");
      closeButton.type = "button";
      closeButton.className = "btn btn-quiet";
      closeButton.textContent = labels.back_to_map;
      closeButton.addEventListener("click", () => clearSelection());
      sizeRow.append(closeButton);
      details.append(sizeRow);
      details.hidden = false;
      state.details = true;
      root.classList.add("details-open");
      document.querySelector("#main")?.classList.add("details-open");
      liveMapController?.resize();
    }

    async function loadAreaDetails(feature, silent = false) {
      const properties = feature?.properties || {};
      const squareId = state.square || properties.cell_id || feature?.id || "";
      const targetDate = state.date || properties.target_date || "";
      const updateId = state.updateId || properties.update_id || "";
      if (!squareId || !targetDate) return;
      invalidateAreaDetails();
      const generation = stateData.detailGeneration;
      const controller = new AbortController();
      stateData.detailRequest = controller;
      stateData.detailFeature = feature;
      // A silent reload keeps the current panel until the replacement arrives.
      if (!silent) renderLoadingDetails(feature);
      const query = new URLSearchParams({ target_date: targetDate });
      if (updateId) query.set("update_id", updateId);
      try {
        const { response, body } = await fetchJson(`/api/v1/areas/${encodeURIComponent(squareId)}?${query}`, { signal: controller.signal });
        if (generation !== stateData.detailGeneration || controller.signal.aborted) return;
        if (!response.ok) {
          if (response.status === 404 || response.status === 409) {
            renderLoadingDetails(feature, classificationText(properties, body), "info");
            addStatusCallout(createLiveDetails(), labels.detail_source_unavailable, "info");
          } else if (response.status === 410 && body?.code === "update_expired") {
            renderLoadingDetails(
              feature,
              labels.detail_update_expired,
              "info",
              refreshSelectedArea,
              labels.detail_refresh_map,
            );
          } else {
            renderDetailError(feature, () => loadAreaDetails(feature));
          }
          return;
        }
        if (!body?.selection || body.selection.square_id !== squareId || body.selection.target_date !== targetDate || (updateId && body.selection.update_id !== updateId)) {
          renderDetailError(feature, () => loadAreaDetails(feature));
          return;
        }
        stateData.detailData = body;
        state.updateId = String(body.selection.update_id || updateId);
        state.assessment = body.selection.assessment_date || state.assessment;
        state.representative = body.selection.representative_coordinates || null;
        state.detailRepresentative = body.selection.representative_coordinates || null;
        state.detailRepresentativeSquare = squareId;
        state.detailRepresentativeDate = targetDate;
        state.detailRepresentativeUpdate = String(body.selection.update_id || updateId);
        persistState();
        renderDetailContent(body, feature);
      } catch (error) {
        if (error.name === "AbortError" || generation !== stateData.detailGeneration) return;
        renderDetailError(feature, () => loadAreaDetails(feature));
      } finally {
        if (generation === stateData.detailGeneration) stateData.detailRequest = null;
      }
    }

    function showDetails(feature, pointSelection = null) {
      const properties = feature.properties || {};
      state.details = true;
      state.square = properties.cell_id || feature.id || "";
      state.updateId = properties.update_id || "";
      const currentDetailMatchesSelection = state.detailRepresentativeSquare === state.square
        && state.detailRepresentativeDate === state.date
        && state.detailRepresentativeUpdate === String(state.updateId || "");
      if ((pointSelection?.source === "map" || pointSelection?.source === "centre") && !currentDetailMatchesSelection) {
        state.representative = null;
        state.detailRepresentative = null;
        state.detailRepresentativeSquare = "";
        state.detailRepresentativeDate = "";
        state.detailRepresentativeUpdate = "";
      }
      state.assessment = properties.assessment_date || state.assessment;
      if (pointSelection?.source === "centre") {
        state.point = null;
        state.pointName = "";
        state.pointSource = "centre";
      } else if (pointSelection?.point) {
        state.point = pointSelection.point;
        if (pointSelection.source === "map") state.pointName = "";
        state.pointSource = pointSelection.source || state.pointSource || "map";
      }
      root.classList.add("details-open");
      document.querySelector("#main")?.classList.add("details-open");
      persistState();
      const loadedDetailMatches = stateData.detailData?.selection?.square_id === state.square
        && stateData.detailData.selection.target_date === state.date
        && String(stateData.detailData.selection.update_id || "") === String(state.updateId || "")
        && (!stateData.detailData.selection.run_id || !stateData.info?.runs?.length
          || stateData.info.runs.some((item) => item.run_id === stateData.detailData.selection.run_id));
      // The same square and date with a new update or run keeps the panel until the replacement arrives.
      const sameSquareAndDate = stateData.detailData?.selection?.square_id === state.square
        && stateData.detailData.selection.target_date === state.date;
      if (!loadedDetailMatches) loadAreaDetails(feature, sameSquareAndDate);
      liveMapController?.resize();
    }

    function clearSelection() {
      beginSelectionIntent();
      clearStoredGeolocation();
      invalidateAreaDetails();
      stateData.weatherLabel?.remove();
      stateData.weatherLabel = null;
      stateData.selected = null;
      stateData.estimateGeneration += 1;
      stateData.estimateRequest?.abort();
      stateData.estimateRequest = null;
      stateData.estimateFeature = null;
      clearDateEstimates();
      clearPendingSelection();
      emptySource("selected-square");
      emptySource("weather-area");
      const details = document.querySelector("aside[data-live-details]");
      details?.remove();
      state.details = false;
      state.square = "";
      state.updateId = "";
      state.point = null;
      state.pointName = "";
      state.representative = null;
      state.detailRepresentative = null;
      state.detailRepresentativeSquare = "";
      state.detailRepresentativeDate = "";
      state.detailRepresentativeUpdate = "";
      state.pointSource = "";
      root.classList.remove("details-open");
      document.querySelector("#main")?.classList.remove("details-open");
      persistState();
      liveMapController?.resize();
      document.getElementById("map")?.focus();
    }

    function selectFeature(feature, pointSelection = null) {
      if (feature.properties?.overview) {
        const ring = feature.geometry.coordinates[0];
        const west = Math.min(...ring.map(point => point[0]));
        const east = Math.max(...ring.map(point => point[0]));
        const south = Math.min(...ring.map(point => point[1]));
        const north = Math.max(...ring.map(point => point[1]));
        stateData.map.fitBounds([[west, south], [east, north]], { padding: 100, maxZoom: 12 });
        return;
      }
      if (pointSelection?.source === "map" || pointSelection?.source === "centre") {
        beginSelectionIntent();
        clearStoredGeolocation();
        stateData.destinationGeneration += 1;
        stateData.destinationRequest?.abort();
        stateData.destinationRequest = null;
      }
      const selection = plainFeature(feature);
      if (!selection?.geometry) return;
      stateData.selected = selection;
      drawSelection(selection);
      showDetails(selection, pointSelection);
      loadSelectedEstimates(selection);
    }

    function visibleSizeKm() {
      const bounds = stateData.map.getBounds();
      const centre = stateData.map.getCenter();
      const width = Math.abs(bounds.getEast() - bounds.getWest()) * 111.32 * Math.max(0.2, Math.cos(centre.lat * Math.PI / 180));
      const height = Math.abs(bounds.getNorth() - bounds.getSouth()) * 111.32;
      return { width, height };
    }

    function outsideCoverage() {
      const coverage = stateData.info?.coverage;
      if (!coverage) return false;
      const values = Array.isArray(coverage)
        ? { west: coverage[0], south: coverage[1], east: coverage[2], north: coverage[3] }
        : coverage;
      const bounds = stateData.map.getBounds();
      return bounds.getEast() <= Number(values.west)
        || bounds.getWest() >= Number(values.east)
        || bounds.getNorth() <= Number(values.south)
        || bounds.getSouth() >= Number(values.north);
    }

    function syncMapState() {
      const centre = stateData.map?.getCenter();
      if (!centre) return;
      const zoom = Number(stateData.map.getZoom().toFixed(2));
      const view = { lat: centre.lat, lng: centre.lng, zoom };
      const changed = !stateData.lastView
        || Math.abs(stateData.lastView.lat - view.lat) > 0.000001
        || Math.abs(stateData.lastView.lng - view.lng) > 0.000001
        || Math.abs(stateData.lastView.zoom - view.zoom) > 0.001;
      stateData.lastView = view;
      state.center = `${centre.lat.toFixed(5)},${centre.lng.toFixed(5)}`;
      state.zoom = zoom;
      persistState();
      return changed;
    }

    async function fetchJson(url, options = {}) {
      const response = await fetch(url, options);
      const body = await response.json().catch(() => null);
      return { response, body };
    }

    async function fetchStyle(name, signal) {
      const { response, body } = await fetchJson(styles[name], { signal, headers: { Accept: "application/json" } });
      if (!response.ok || !body || body.version !== 8 || !Array.isArray(body.layers) || !body.sources) throw new Error("style");
      return { ...body, metadata: { ...(body.metadata || {}), sporameteoStyle: name } };
    }

    async function fetchForest(generation) {
      if (!stateData.map || generation !== stateData.generation) return false;
      if (stateData.forest) {
        setForestVisible(true);
        return true;
      }
      if (!stateData.forestRequest) {
        message(labels.map_loading, "wait");
        stateData.forestRequest = (async () => {
          try {
            const { response, body } = await fetchJson("/api/v1/forest-layer", { headers: { Accept: "application/geo+json" } });
            if (!response.ok || !body?.features) throw new Error("forest-layer");
            return body;
          } catch (error) {
            return null;
          } finally {
            stateData.forestRequest = null;
          }
        })();
      }
      const body = await stateData.forestRequest;
      if (generation !== stateData.generation) return false;
      if (!body) {
        message(labels.map_layer_error, "error", true, () => scheduleFetch(0));
        return false;
      }
      stateData.forest = body;
      stateData.map.getSource("forest")?.setData(body);
      setForestVisible(true);
      return true;
    }

    function dataForDate(data, targetDate) {
      const features = data.features.filter((feature) => feature.properties?.target_date === targetDate);
      return { ...data, features };
    }

    function keepDestinationWithoutFeature(selectedId, selectedUpdateId) {
      const hasDestination = Boolean(state.point || state.representative || state.pointSource);
      if (!hasDestination) {
        clearPendingSelection();
        clearSelection();
        return false;
      }
      clearVisibleSelection();
      state.details = false;
      state.square = selectedId;
      state.updateId = selectedUpdateId || "";
      clearPendingSelection();
      root.classList.remove("details-open");
      document.querySelector("#main")?.classList.remove("details-open");
      persistState();
      return true;
    }

    function handleSelectionAfterData(data, runChanged = false) {
      const selectedId = state.square || stateData.pendingSelectionId;
      if (!selectedId) return "";
      // A new run brings new update ids, so the old one is not a sign of expiry.
      const selectedUpdateId = runChanged ? "" : state.updateId || stateData.pendingSelectionUpdateId;
      const feature = data.features.find((item) => item.id === selectedId || item.properties?.cell_id === selectedId);
      if (!feature) {
        if (stateData.pendingFavoriteCell === selectedId) {
          stateData.pendingFavoriteCell = null;
          state.square = selectedId;
          state.details = true;
          clearPendingSelection();
          persistState();
          message(labels.favorite_missing, "error");
          renderFavoriteMissing(selectedId);
          return "";
        }
        // A selected area remains selected when it leaves the viewport.
        if (stateData.selected?.properties?.target_date === state.date && state.details) return "";
        keepDestinationWithoutFeature(selectedId, selectedUpdateId);
        return "";
      }
      if (feature.properties?.state === "expired" || (selectedUpdateId && feature.properties?.update_id && feature.properties.update_id !== selectedUpdateId)) {
        keepDestinationWithoutFeature(selectedId, selectedUpdateId);
        return labels.map_expired;
      }
      state.square = selectedId;
      state.updateId = feature.properties?.update_id || selectedUpdateId || "";
      state.details = state.details || stateData.pendingSelectionDetails;
      state.point = stateData.pendingSelectionPoint || state.point;
      state.representative = stateData.detailData?.selection?.square_id === selectedId
        && stateData.detailData.selection.target_date === state.date
        ? stateData.detailData.selection.representative_coordinates
        : ["map", "centre"].includes(stateData.pendingSelectionSource)
          ? null
          : stateData.pendingSelectionRepresentative || state.representative;
      state.pointSource = stateData.pendingSelectionSource || state.pointSource;
      clearPendingSelection();
      persistState();
      stateData.selected = feature;
      drawSelection(feature);
      loadSelectedEstimates(feature);
      if (state.details) showDetails(feature, {
        point: state.point,
        source: state.pointSource,
      });
      return "";
    }

    function providerReasonMessage(features) {
      const reasons = features
        .map((feature) => feature.properties?.reason || feature.properties?.unavailable_reason)
        .filter(Boolean);
      for (const reason of ["visitor_limit", "quota_exhausted", "provider_refused", "provider_unavailable"]) {
        const text = providerReasonText(reason);
        if (reasons.includes(reason)) return text;
      }
      return "";
    }

    function providerReasonText(reason) {
      return {
        visitor_limit: labels.map_visitor_limit,
        quota_exhausted: labels.map_quota_exhausted,
        provider_refused: labels.map_provider_refused,
        provider_unavailable: labels.map_provider_unavailable,
      }[reason] || "";
    }

    async function fetchSquares(generation) {
      if (!stateData.info || !stateData.map || generation !== stateData.generation) return;
      if (stateData.request) stateData.request.abort();
      const size = visibleSizeKm();
      const overview = size.width > Number(stateData.info.max_width_km) || size.height > Number(stateData.info.max_height_km);
      setForestVisible(false);
      if (outsideCoverage()) {
        const missingFavoriteCell = stateData.pendingFavoriteCell;
        clearSquares();
        if (missingFavoriteCell) {
          stateData.pendingFavoriteCell = null;
          clearPendingSelection();
          state.square = missingFavoriteCell;
          state.details = true;
          persistState();
          renderFavoriteMissing(missingFavoriteCell);
          message(labels.favorite_missing, "error");
          return;
        }
        message(labels.map_outside_coverage, "error");
        return;
      }
      const bounds = stateData.map.getBounds();
      const bbox = [bounds.getWest(), bounds.getSouth(), bounds.getEast(), bounds.getNorth()].map((value) => value.toFixed(6)).join(",");
      const query = new URLSearchParams({ bbox, target_date: state.date });
      const controller = new AbortController();
      stateData.request = controller;
      stateData.requestCount += 1;
      const requestGeneration = generation;
      message((labels.map_loading_estimates || labels.loading).replace("{date}", fullDate(state.date)), "wait");
      try {
        const { response, body } = await fetchJson(`/api/v1/${overview ? "estimate-overview" : "squares"}?${query}`, { signal: controller.signal });
        if (requestGeneration !== stateData.generation || controller.signal.aborted) return;
        if (!response.ok) {
          const code = body?.code;
          if (code === "view_too_large" || code === "square_limit_exceeded") message(labels.map_zoom_in, "wait");
          else if (code === "outside_coverage") message(labels.map_outside_coverage, "error");
          else if (code === "forecast_unavailable") message(labels.map_forecast_unavailable, "error", true, () => scheduleFetch(0));
          else if (providerReasonText(code)) message(providerReasonText(code), "error", true, () => scheduleFetch(0));
          else message(labels.map_request_error, "error", true, () => scheduleFetch(0));
          return;
        }
        if (!body || !Array.isArray(body.features)) throw new Error("squares");
        stateData.request = null;
        if (body.target_date && body.target_date !== state.date) return;
        const currentRunId = stateData.info.run?.run_id ?? null;
        if (body.run_id && currentRunId && body.run_id !== currentRunId) {
          // The info answer is older than this response, so check the info once for this run.
          if (stateData.runRecheckId !== body.run_id) {
            stateData.runRecheckId = body.run_id;
            refreshInfo().catch(() => {});
          }
          return;
        }
        stateData.lastRequest = { targetDate: state.date, bbox, generation: requestGeneration };
        stateData.squares = dataForDate(body, state.date);
        stateData.displayedDate = state.date;
        const runChanged = Boolean(stateData.displayedRunId && body.run_id && stateData.displayedRunId !== body.run_id);
        stateData.displayedRunId = body.run_id ?? null;
        stateData.map.getSource("squares")?.setData(stateData.squares);
        stateData.squareSetCount += 1;
        updateStale();
        updateDateMetadata(stateData.squares.features);
        stateData.renderCount += 1;
        const selectionMessage = overview ? "" : handleSelectionAfterData(stateData.squares, runChanged);
        revealSquaresWhenRendered();
        const providerMessage = providerReasonMessage(stateData.squares.features);
        if (selectionMessage) {
          message(selectionMessage, "older");
        } else if (providerMessage) {
          message(providerMessage, "error", true, () => scheduleFetch(0));
        } else if (!stateData.squares.features.length) {
          message(labels.map_no_results, "wait");
        } else {
          message(overview ? (labels.map_overview || "Area averages. Zoom in for individual scores.") : "", "wait");
        }
      } catch (error) {
        if (error.name === "AbortError") return;
        if (requestGeneration !== stateData.generation) return;
        message(labels.map_request_error, "error", true, () => scheduleFetch(0));
      }
    }

    function scheduleFetch(delay = 280) {
      window.clearTimeout(stateData.debounce);
      stateData.generation += 1;
      if (stateData.request) stateData.request.abort();
      const generation = stateData.generation;
      stateData.debounce = window.setTimeout(() => fetchSquares(generation), delay);
    }

    function bindMapEvents() {
      const map = stateData.map;
      const invalidateMovement = () => {
        if (stateData.movementInvalidated) return;
        stateData.invalidationCount += 1;
        stateData.movementInvalidated = true;
        stateData.generation += 1;
        stateData.request?.abort();
        setForestVisible(false);
      };
      listen("movestart", () => {
        stateData.moveStarts += 1;
        window.cancelAnimationFrame(stateData.moveFrame);
        stateData.movementInvalidated = false;
        stateData.moveFrame = window.requestAnimationFrame(() => {
          const centre = map.getCenter();
          const zoom = Number(map.getZoom().toFixed(2));
          const view = stateData.lastView;
          const changed = !view
            || Math.abs(view.lat - centre.lat) > 0.000001
            || Math.abs(view.lng - centre.lng) > 0.000001
            || Math.abs(view.zoom - zoom) > 0.001;
          if (changed) invalidateMovement();
        });
      });
      listen("moveend", () => {
        window.cancelAnimationFrame(stateData.moveFrame);
        stateData.moveEnds += 1;
        const changed = syncMapState();
        if (changed && !stateData.movementInvalidated) invalidateMovement();
        if (changed || stateData.movementInvalidated) scheduleFetch(280);
        stateData.movementInvalidated = false;
      });
      listen("click", (event) => {
        // Squares of another date are dimmed and not selectable.
        if (root.dataset.mapStale === "true") return;
        const features = map.queryRenderedFeatures(event.point, { layers: ["sporameteo-square-estimates", "sporameteo-square-missing", "sporameteo-square-noforest", "sporameteo-noforest-line", "sporameteo-missing-line"] });
        if (features[0] && event.lngLat) {
          selectFeature(features[0], {
            point: { latitude: event.lngLat.lat, longitude: event.lngLat.lng },
            source: "map",
          });
        }
      });
      listen("mouseenter", () => { map.getCanvas().style.cursor = "pointer"; }, "sporameteo-square-estimates");
      listen("mouseleave", () => { map.getCanvas().style.cursor = ""; }, "sporameteo-square-estimates");
    }

    async function refreshInfo() {
      const { response, body: latest } = await fetchJson("/api/v1/info");
      if (!response.ok) throw new Error("info-refresh");
      if (latest?.today && latest.today !== stateData.info.today) {
        window.location.reload();
        return;
      }
      const latestRunId = latest?.run?.run_id ?? null;
      if (latestRunId === (stateData.info.run?.run_id ?? null) || !latest?.target_dates?.length) return;
      // A new run replaces the dates, then the view and open details load again and swap together.
      stateData.info = latest;
      stateData.runRecheckId = latestRunId;
      if (!latest.target_dates.includes(state.date)) state.date = latest.target_dates[0];
      renderDates(latest.target_dates);
      stateData.estimateCache.clear();
      scheduleFetch(0);
    }

    async function initialise() {
      if (stateData.initialising || stateData.map) return;
      stateData.initialising = true;
      message(labels.map_loading, "wait");
      // The server renders today, which is wrong when the published run is older.
      updateDateMetadata([], true);
      let styleFailed = false;
      try {
        const { response, body } = await fetchJson("/api/v1/info");
        if (!response.ok || !body?.target_dates?.length) throw new Error("info");
        stateData.info = body;
        window.clearTimeout(stateData.dateTimer);
        stateData.dateTimer = window.setTimeout(async function refreshDates() {
          try {
            await refreshInfo();
          } catch (error) {
            message(labels.map_info_error, "error", true, () => window.location.reload());
          } finally {
            stateData.dateTimer = window.setTimeout(refreshDates, 900_000);
          }
        }, 900_000);
        state.assessment = body.today;
        if (!body.target_dates.includes(state.date)) state.date = body.target_dates[0];
        renderDates(body.target_dates);
        styleFailed = true;
        const initialStyle = await fetchStyle(state.background);
        styleFailed = false;
        const mapCenter = parseCenter(state.center) || [11.0, 43.7];
        const options = {
          container,
          style: initialStyle,
          center: mapCenter,
          zoom: state.zoom,
          attributionControl: false,
          // The container observer below handles size changes without a delayed second resize.
          trackResize: false,
          maxZoom: 18,
        };
        stateData.map = new window.maplibregl.Map(options);
        stateData.backgroundKey = state.background;
        if (window.ResizeObserver) {
          const resizeTarget = container.closest(".mapcell") || container;
          stateData.resizeObserver = new ResizeObserver((entries) => {
            const rectangle = entries[0]?.contentRect;
            if (!rectangle) return;
            const width = Math.round(rectangle.width);
            const height = Math.round(rectangle.height);
            if (width === stateData.resizeWidth && height === stateData.resizeHeight) return;
            stateData.resizeWidth = width;
            stateData.resizeHeight = height;
            controller.resize();
          });
          stateData.resizeObserver.observe(resizeTarget);
        }
        stateData.map.addControl(new window.maplibregl.AttributionControl({ compact: true, customAttribution: "OpenStreetMap contributors · OpenFreeMap" }), "bottom-right");
        // MapLibre opens the compact credits on start. They stay closed until the info button opens them.
        container.querySelector(".maplibregl-ctrl-attrib")?.classList.remove("maplibregl-compact-show");
        listen("style.load", restoreCustomLayers);
        listen("styledata", restoreCustomLayers);
        listenOnce("load", () => {
          restoreCustomLayers();
          bindMapEvents();
          stateData.ready = true;
          syncMapState();
          scheduleFetch(0);
          if (stateData.pendingDestination) {
            const destination = stateData.pendingDestination;
            stateData.pendingDestination = null;
            if (destination.favorite) openFavorite(destination.favorite);
            else selectDestination(destination);
          }
        });
        const attribution = document.querySelector("[data-map-attribution]");
        if (attribution) {
          attribution.replaceChildren();
          const mapCredits = document.createElement("span");
          mapCredits.className = "attribution-map";
          const credits = [
            ["OpenStreetMap", "https://www.openstreetmap.org/copyright"],
            ["OpenMapTiles", "https://openmaptiles.org"],
            ["OpenFreeMap", "https://openfreemap.org"],
          ];
          credits.forEach(([text, url], index) => {
            if (index) mapCredits.append(" · ");
            if (typeof url === "string" && /^https?:\/\//.test(url)) {
              const link = document.createElement("a");
              link.href = url;
              link.rel = "noreferrer";
              link.textContent = text;
              mapCredits.append(link);
            } else {
              mapCredits.append(document.createTextNode(text));
            }
          });
          attribution.append(mapCredits);

          const apiCredits = Array.isArray(body.attributions) ? body.attributions : [];
          if (apiCredits.length) {
            const separator = document.createElement("span");
            separator.className = "attribution-sep";
            separator.textContent = " · ";
            attribution.append(separator);
            const details = document.createElement("details");
            details.className = "attribution-details";
            const summary = document.createElement("summary");
            summary.textContent = labels.map_api_credit;
            details.append(summary);
            const detailsBody = document.createElement("div");
            detailsBody.className = "attribution-details-body";
            apiCredits.forEach((item) => {
              const source = document.createElement("div");
              source.className = "attribution-source";
              if (item.name) {
                const name = document.createElement("strong");
                name.textContent = item.name;
                source.append(name);
              }
              [item.credit, item.licence].forEach((text) => {
                if (!text) return;
                const line = document.createElement("div");
                if (typeof item.url === "string" && /^https?:\/\//.test(item.url)) {
                  const link = document.createElement("a");
                  link.href = item.url;
                  link.rel = "noreferrer";
                  link.textContent = text;
                  line.append(link);
                } else {
                  line.append(document.createTextNode(text));
                }
                source.append(line);
              });
              detailsBody.append(source);
            });
            details.append(detailsBody);
            attribution.append(details);
          }
        }
      } catch (error) {
        message(
          styleFailed ? labels.map_style_error : labels.map_info_error,
          "error",
          true,
          () => initialise(),
        );
      } finally {
        stateData.initialising = false;
      }
    }

    async function selectDestination(destination) {
      const latitude = Number(destination?.latitude);
      const longitude = Number(destination?.longitude);
      if (!Number.isFinite(latitude) || !Number.isFinite(longitude)
        || latitude < -90 || latitude > 90 || longitude < -180 || longitude > 180) return false;
      const point = { latitude, longitude };
      const source = destination.source || "point";
      if (source !== "geolocation") clearStoredGeolocation();
      const destinationGeneration = ++stateData.destinationGeneration;
      stateData.destinationRequest?.abort();
      const destinationController = new AbortController();
      stateData.destinationRequest = destinationController;
      clearVisibleSelection();
      state.square = "";
      state.updateId = "";
      state.details = false;
      state.representative = null;
      clearPendingSelection();
      root.classList.remove("details-open");
      document.querySelector("#main")?.classList.remove("details-open");
      state.point = point;
      state.pointName = typeof destination.name === "string" ? destination.name.trim() : "";
      state.pointSource = source;
      stateData.pendingDestination = !stateData.map || !stateData.ready
        ? {
          latitude,
          longitude,
          source,
          name: state.pointName,
          preservePointOnFailure: destination.preservePointOnFailure === true,
        }
        : null;
      persistState();
      if (!stateData.map || !stateData.ready) return false;
      const centre = [longitude, latitude];
      const targetZoom = Math.max(12, Math.min(18, stateData.map.getZoom()));
      let response;
      let body;
      try {
        ({ response, body } = await fetchJson("/api/v1/places/resolve", {
          method: "POST",
          credentials: "same-origin",
          headers: {
            Accept: "application/json",
            "Content-Type": "application/json",
          },
          body: JSON.stringify({ latitude, longitude, source }),
          signal: destinationController.signal,
        }));
      } catch (error) {
        if (error.name === "AbortError" || destinationGeneration !== stateData.destinationGeneration) return false;
        if (destination.preservePointOnFailure === true) stateData.map.easeTo({ center: centre, zoom: targetZoom });
        message(labels.search_error, "error");
        return false;
      } finally {
        if (destinationGeneration === stateData.destinationGeneration) {
          stateData.destinationRequest = null;
        }
      }
      if (destinationGeneration !== stateData.destinationGeneration || destinationController.signal.aborted) return false;
      if (!response.ok) {
        state.square = "";
        state.details = false;
        const errorMessage = body?.code === "outside_italy"
          ? labels.outside_italy
          : body?.code === "outside_configured_coverage"
            ? labels.outside_coverage
            : labels.search_error;
        if (destination.preservePointOnFailure === true) stateData.map.easeTo({ center: centre, zoom: targetZoom });
        message(errorMessage, "error");
        persistState();
        return false;
      }
      state.square = body.square_id || "";
      state.updateId = "";
      state.representative = body.representative || null;
      state.details = Boolean(state.square);
      persistState();
      const currentCentre = stateData.map.getCenter();
      const sameView = currentCentre
        && Math.abs(currentCentre.lng - longitude) < 0.000001
        && Math.abs(currentCentre.lat - latitude) < 0.000001
        && Math.abs(stateData.map.getZoom() - targetZoom) < 0.001;
      stateData.map.easeTo({ center: centre, zoom: targetZoom });
      if (state.square && (sameView || !stateData.map.isMoving())) scheduleFetch(0);
      return true;
    }

    async function openFavorite(favorite) {
      const valid = window.SporaMeteoFavorites?.validateItem?.(favorite);
      if (!valid) {
        message(labels.favorite_invalid, "error");
        return false;
      }
      beginSelectionIntent();
      if (valid.kind === "point") {
        return selectDestination({
          latitude: valid.latitude,
          longitude: valid.longitude,
          source: "point",
          name: valid.name,
          preservePointOnFailure: true,
        });
      }
      if (!stateData.map || !stateData.ready) {
        stateData.pendingDestination = { favorite: valid };
        return false;
      }
      clearStoredGeolocation();
      stateData.destinationGeneration += 1;
      stateData.destinationRequest?.abort();
      stateData.destinationRequest = null;
      clearVisibleSelection();
      clearPendingSelection();
      stateData.pendingFavoriteCell = valid.cellId;
      state.square = valid.cellId;
      state.updateId = "";
      state.details = true;
      state.point = { latitude: valid.latitude, longitude: valid.longitude };
      state.pointName = "";
      state.representative = null;
      state.pointSource = "centre";
      root.classList.remove("details-open");
      document.querySelector("#main")?.classList.remove("details-open");
      persistState();
      const targetZoom = Math.max(13, Math.min(18, stateData.map.getZoom()));
      const currentCentre = stateData.map.getCenter();
      const sameView = currentCentre
        && Math.abs(currentCentre.lng - valid.longitude) < 0.000001
        && Math.abs(currentCentre.lat - valid.latitude) < 0.000001
        && Math.abs(stateData.map.getZoom() - targetZoom) < 0.001;
      stateData.map.easeTo({ center: [valid.longitude, valid.latitude], zoom: targetZoom });
      // A request sent while the map moves would use the old view, so moveend fetches instead.
      if (sameView || !stateData.map.isMoving()) scheduleFetch(0);
      return true;
    }

    const controller = {
      zoom(delta) {
        if (!stateData.map) return;
        stateData.map.easeTo({ zoom: Math.max(1, Math.min(18, stateData.map.getZoom() + delta)) });
      },
      setDate(targetDate) {
        if (stateData.squares && stateData.displayedDate !== targetDate) updateDateMetadata([], true);
        updateStale();
        if (stateData.selected && state.details && targetDate !== stateData.detailData?.selection?.target_date) {
          // Show the loading state at once so old values never sit under the new date.
          invalidateAreaDetails();
          renderLoadingDetails(stateData.selected);
        }
        if (stateData.ready) scheduleFetch();
      },
      setOverlay(value) { applyOverlay(value); },
      setOpacity(value) { applyOpacity(value); },
      selectDestination,
      openFavorite,
      closeDetails() {
        if (state.details) clearSelection();
      },
      centerSelection() {
        const map = stateData.map;
        if (!map || !stateData.squaresRendered) return false;
        const point = map.project(map.getCenter());
        const features = map.queryRenderedFeatures(point, {
          layers: ["sporameteo-square-estimates", "sporameteo-square-missing", "sporameteo-square-noforest", "sporameteo-noforest-line", "sporameteo-missing-line"],
        });
        if (!features[0]) return false;
        const centre = featureCenter(features[0]);
        selectFeature(features[0], { source: "centre" });
        return true;
      },
      getFavoriteSelection() {
        if (["search", "geolocation", "point"].includes(state.pointSource) && state.point) {
          return {
            kind: "point",
            latitude: state.point.latitude,
            longitude: state.point.longitude,
            name: state.pointName,
          };
        }
        if (state.square) {
          const centre = stateData.selected ? featureCenter(stateData.selected) : null;
          const point = centre
            ? { latitude: centre[1], longitude: centre[0] }
            : state.point;
          if (!point) return null;
          return {
            kind: "grid",
            latitude: point.latitude,
            longitude: point.longitude,
            cellId: state.square,
            name: state.pointName,
          };
        }
        if (state.point) {
          return {
            kind: "point",
            latitude: state.point.latitude,
            longitude: state.point.longitude,
            name: state.pointName,
          };
        }
        return null;
      },
      resize() {
        const width = container.clientWidth;
        const height = container.clientHeight;
        if (width === stateData.appliedResizeWidth && height === stateData.appliedResizeHeight) return;
        stateData.appliedResizeWidth = width;
        stateData.appliedResizeHeight = height;
        stateData.map?.resize();
      },
      async setBackground(value, previousValue = stateData.backgroundKey) {
        if (!stateData.map || !styles[value]) return;
        const generation = ++stateData.styleGeneration;
        stateData.styleRequest?.abort();
        if (value === stateData.backgroundKey) {
          stateData.styleRequest = null;
          return;
        }
        const request = new AbortController();
        stateData.styleRequest = request;
        try {
          const style = await fetchStyle(value, request.signal);
          if (generation !== stateData.styleGeneration || request.signal.aborted) return;
          stateData.map.setStyle(style, { diff: false });
          stateData.map.once("idle", restoreCustomLayers);
          stateData.backgroundKey = value;
          state.background = value;
          root.classList.toggle("bg-contrast", state.background === "contrast");
          document.querySelectorAll("input[name=background]").forEach((input) => { input.checked = input.value === state.background; });
          persistState();
          const styleMessageId = stateData.styleMessageId;
          stateData.styleMessageId = null;
          const current = currentMessage();
          if (
            styleMessageId
            && current?.id === styleMessageId
            && current.source === "style"
          ) {
            message("");
          }
        } catch (error) {
          if (error.name === "AbortError" || generation !== stateData.styleGeneration) return;
          state.background = previousValue;
          root.classList.toggle("bg-contrast", state.background === "contrast");
          document.querySelectorAll("input[name=background]").forEach((input) => { input.checked = input.value === state.background; });
          persistState();
          const failedStyle = value;
          stateData.styleMessageId = message(
            labels.map_style_error,
            "error",
            true,
            () => controller.setBackground(failedStyle, stateData.backgroundKey),
            "style",
          );
        } finally {
          if (generation === stateData.styleGeneration) stateData.styleRequest = null;
        }
      },
      getDiagnostics() {
        return {
          mapInstances: stateData.map ? 1 : 0,
          mapInstanceCount: stateData.map ? 1 : 0,
          listenerCount: stateData.listenerCount,
          requestCount: stateData.requestCount,
          renderCount: stateData.renderCount,
          squareSetCount: stateData.squareSetCount,
          displayedDate: stateData.displayedDate,
          displayedRunId: stateData.displayedRunId,
          styleLoads: stateData.styleLoads,
          generation: stateData.generation,
          moveStarts: stateData.moveStarts,
          moveEnds: stateData.moveEnds,
          invalidationCount: stateData.invalidationCount,
          ready: stateData.ready,
          targetDate: state.date,
          representative: state.representative,
          detailRepresentative: state.detailRepresentative,
          background: state.background,
          overlay: state.overlay,
          opacity: state.opacity,
          lastTargetDate: stateData.lastRequest?.targetDate || null,
        };
      },
    };
    Object.defineProperty(controller, "map", { get: () => stateData.map });
    Object.defineProperty(controller, "lastRequest", { get: () => stateData.lastRequest });
    window.SporaMeteoMap = controller;
    initialise();
    return controller;
  }

  const placeSearch = createPlaceSearch();
  liveMapController = createLiveMapController();
  if (window.__sporameteoPendingDestination && liveMapController?.selectDestination) {
    liveMapController.selectDestination(window.__sporameteoPendingDestination);
    delete window.__sporameteoPendingDestination;
  }
  window.SporaMeteoPlaceSearch = placeSearch;
})();
