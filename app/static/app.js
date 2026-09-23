/* No external scripts: the API owns filtering, ranking, and factual explanations. */
"use strict";

const $ = (selector) => document.querySelector(selector);
const form = $("#search-form");
const resultsPanel = $(".results-panel");
const searchButton = $("#search-button");
const resultContent = $("#result-content");
const errorPanel = $("#error-panel");
const statusLine = $("#status-line");
const compareButton = $("#compare-button");
const money = new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 2 });
const day = new Intl.DateTimeFormat("ru-RU", { day: "numeric", month: "long" });
const fullDay = new Intl.DateTimeFormat("ru-RU", { day: "numeric", month: "long", year: "numeric" });
const REASONS = { busy: "Заняты", budget: "Бюджет", format: "Формат", duration: "Часы", language: "Язык" };
const FIELD_NAMES = { city: "Город", date: "Дата", event_format: "Формат", category: "Категория", budget: "Бюджет", duration: "Длительность", language: "Язык" };
let metadata = null;
let lastRequest = null;
let searchSequence = 0;
let searchController = null;
let comparisonSequence = 0;
let comparisonControllers = [];
let formRevision = 0;
let inquirySelection = null;
let inquiryAttempt = null;
let inquirySending = false;

function inquiryBrief(card, request) {
  return ["Обращение в команду сервиса «событие.»", `Профиль: ${card.anon_name} (${card.id})`,
    `Условия: ${summary(request)}`, `Начальная цена в каталоге: ${price(card.price_from_kzt)}`,
    "", card.synthetic ? "Учебный синтетический профиль. Реальный заказ недоступен." : "Анонимизированный профиль. Контакт исполнителя должен подтвердить сотрудник сервиса.",
    "Просьба уточнить доступность, итоговую стоимость, состав услуг и порядок заключения договора.",
    "Этот файл — подготовленные условия события. Он не отправлен и не подтверждает бронирование."].join("\n");
}

function openInquiry(card, request) {
  if (inquirySending) return;
  inquirySelection = { card, request: { ...request }, brief: inquiryBrief(card, request) };
  inquiryAttempt = null;
  const enabled = !card.synthetic && metadata?.inquiries?.enabled === true;
  $("#inquiry-form").reset();
  $("#inquiry-fields").disabled = !enabled;
  $("#inquiry-submit").disabled = !enabled;
  $("#inquiry-form").hidden = !enabled;
  $("#inquiry-title").textContent = card.synthetic ? "Пример обращения" : "Обращение в команду";
  $("#inquiry-brief").textContent = inquirySelection.brief;
  $("#inquiry-notice").textContent = card.synthetic
    ? "Это синтетический профиль для демонстрации. Заказать его услуги нельзя; можно скачать пример условий."
    : enabled
      ? `Обращение получит команда сервиса через ${metadata.inquiries.channel_label}. Сотрудник проверит возможность заказа и свяжется с вами.`
      : "Канал команды ещё не подключён. Сейчас можно скачать условия события; отправка и сбор контактов пока недоступны.";
  $("#inquiry-status").replaceChildren();
  $("#inquiry-dialog").showModal();
}

function closeInquiry() {
  if (inquirySending) return;
  $("#inquiry-dialog").close();
  $("#inquiry-form").reset();
  inquirySelection = null;
  inquiryAttempt = null;
}

function showInquiryReceipt(receipt) {
  const root = $("#inquiry-status");
  root.replaceChildren(el("p", "", receipt.message), el("p", "", `Номер обращения: ${receipt.id}`));
  $("#inquiry-fields").disabled = true;
  $("#inquiry-submit").disabled = true;
  if (receipt.receipt_token) {
    const check = el("button", "guidance-button", "Проверить статус");
    check.type = "button";
    check.addEventListener("click", async () => {
      const selected = inquirySelection;
      check.disabled = true;
      try {
        const response = await fetch(`/api/inquiries/${encodeURIComponent(receipt.id)}`, { headers: { Authorization: `Bearer ${receipt.receipt_token}` }, cache: "no-store", signal: AbortSignal.timeout(10000) });
        if (!response.ok) throw new Error();
        const updated = await response.json();
        if (selected !== inquirySelection) return;
        showInquiryReceipt({ ...updated, receipt_token: receipt.receipt_token });
      } catch {
        if (selected !== inquirySelection) return;
        root.append(el("p", "", "Не удалось проверить статус. Обращение повторно не отправлялось.")); check.disabled = false;
      }
    });
    root.append(check);
  }
}

async function submitInquiry(event) {
  event.preventDefault();
  if (inquirySending || !inquirySelection || inquirySelection.card.synthetic || !metadata?.inquiries?.enabled) return;
  if (!inquiryAttempt) {
    if (!$("#inquiry-form").reportValidity()) return;
    const values = new FormData($("#inquiry-form"));
    inquiryAttempt = { key: crypto.randomUUID(), payload: {
      contractor_id: inquirySelection.card.id, search: inquirySelection.request,
      name: String(values.get("name") || "").trim(), contact: String(values.get("contact") || "").trim(),
      message: String(values.get("message") || "").trim(), consent: values.get("consent") === "on",
    } };
  }
  inquirySending = true;
  $("#inquiry-fields").disabled = true;
  $("#inquiry-submit").disabled = true;
  $("#inquiry-close").disabled = true;
  $("#inquiry-status").textContent = "Сохраняем обращение и проверяем передачу команде…";
  try {
    const response = await fetch("/api/inquiries", { method: "POST", headers: { "Content-Type": "application/json", "Idempotency-Key": inquiryAttempt.key }, body: JSON.stringify(inquiryAttempt.payload), signal: AbortSignal.timeout(15000) });
    const body = await response.json();
    if (!response.ok) {
      if (response.status >= 400 && response.status < 500) {
        inquiryAttempt = null;
        $("#inquiry-fields").disabled = false;
      }
      throw new Error(body.message || "Не удалось принять обращение. Проверьте поля и повторите.");
    }
    showInquiryReceipt(body);
  } catch (error) {
    $("#inquiry-status").textContent = error.name === "TimeoutError" || error instanceof TypeError
      ? "Ответ не получен. Повторная попытка с теми же данными проверит сохранённое обращение, не создавая дубликат."
      : error.message;
    $("#inquiry-submit").disabled = false;
  } finally {
    inquirySending = false;
    $("#inquiry-close").disabled = false;
  }
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function add(parent, ...children) {
  children.filter(Boolean).forEach((child) => parent.append(child));
  return parent;
}

function dateObject(value) {
  // Local noon prevents an ISO date from moving a day in a negative timezone.
  const [year, month, date] = String(value).split("-").map(Number);
  return new Date(year, month - 1, date, 12);
}

function formatDate(value, full = false) {
  const date = dateObject(value);
  return Number.isNaN(date.getTime()) ? String(value) : (full ? fullDay : day).format(date);
}

function price(value) {
  return `${money.format(Number(value))} ₸`;
}

function countWord(number, forms) {
  const n = Math.abs(number) % 100;
  const last = n % 10;
  return forms[n > 10 && n < 20 ? 2 : last === 1 ? 0 : last >= 2 && last <= 4 ? 1 : 2];
}

function sourceName(value) {
  return { astra: "Astra · проверенные факты", openai: `${metadata?.runtime?.explanation_model || "OpenAI"} · проверенные факты`, fallback_llm: "Резервная LLM · проверенные факты", template: "Объяснение по фактам" }[value] || "Объяснение по фактам";
}

function runtimeName(value) {
  if (!value) return "Объяснения по фактам";
  const lower = String(value).toLowerCase();
  if (lower === "template" || lower.includes("template_only")) return "Объяснения по фактам";
  if (lower.includes("astra")) return "Astra с проверкой фактов";
  if (lower.includes("openai")) return `${metadata?.runtime?.explanation_model || "OpenAI"} с проверкой фактов`;
  if (lower.includes("fallback") || lower.includes("llm")) return "LLM с проверкой фактов";
  return "Объяснения по фактам";
}

function isSemanticMode(mode) {
  return mode === "semantic" || mode === "frozen_semantic";
}

class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

async function callApi(path, payload, controller = new AbortController()) {
  let timedOut = false;
  const timeout = setTimeout(() => { timedOut = true; controller.abort(); }, 12000);
  try {
    const response = await fetch(path, {
      method: payload ? "POST" : "GET",
      headers: { Accept: "application/json", ...(payload ? { "Content-Type": "application/json" } : {}) },
      body: payload ? JSON.stringify(payload) : undefined,
      signal: controller.signal,
      cache: "no-store",
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) {
      if (response.status === 422 && Array.isArray(body.errors)) {
        const errors = body.errors.map((item) => {
          const field = FIELD_NAMES[item.field] || "Параметры события";
          return `${field}: ${item.message || "проверьте значение"}`;
        });
        throw new ApiError(errors.length ? errors.join("; ") : (body.message || "Проверьте параметры события."), 422);
      }
      if (response.status === 422 && Array.isArray(body.detail)) {
        const fields = [...new Set(body.detail.map((item) => {
          const field = Array.isArray(item.loc) ? item.loc[item.loc.length - 1] : "";
          return FIELD_NAMES[field] || "Параметры события";
        }))];
        throw new ApiError(`Проверьте поля: ${fields.join(", ")}. Дата должна входить в период каталога, бюджет — быть положительным числом, длительность — целым числом часов.`, 422);
      }
      if (response.status === 503) throw new ApiError("Каталог временно недоступен. Попробуйте повторить подбор через несколько секунд.", 503);
      if (response.status === 504) throw new ApiError("Подбор занял больше времени, чем ожидалось. Повторите запрос: ваши условия сохранены.", 504);
      throw new ApiError("Не удалось получить подборку. Повторите запрос: ваши условия сохранены.", response.status);
    }
    return body;
  } catch (error) {
    if (timedOut) throw new ApiError("Сервис не ответил за 12 секунд. Проверьте соединение и повторите запрос.", 0);
    if (error.name === "AbortError" || error instanceof ApiError) throw error;
    throw new ApiError("Не удаётся связаться с сервисом. Проверьте, что приложение запущено, и повторите запрос.", 0);
  } finally {
    clearTimeout(timeout);
  }
}

function setOptions(select, values, emptyLabel) {
  select.replaceChildren();
  if (emptyLabel) select.append(new Option(emptyLabel, ""));
  values.forEach((value) => select.append(new Option(value, value)));
}

function applyRequest(request) {
  for (const field of Object.keys(FIELD_NAMES)) {
    if (form.elements[field]) form.elements[field].value = request[field] ?? "";
  }
  formRevision += 1;
  markComparisonStale();
}

function readRequest() {
  const values = new FormData(form);
  const budget = Number(values.get("budget"));
  const durationText = String(values.get("duration") || "");
  const duration = durationText ? Number(durationText) : null;
  if (!Number.isFinite(budget) || budget <= 0) throw new ApiError("Укажите бюджет — положительное число в тенге.", 422);
  if (duration !== null && (!Number.isInteger(duration) || duration <= 0)) throw new ApiError("Укажите длительность целым положительным числом часов или оставьте поле пустым.", 422);
  return {
    city: String(values.get("city")),
    date: String(values.get("date")),
    event_format: String(values.get("event_format")),
    category: String(values.get("category")),
    budget,
    duration,
    language: values.get("language") || null,
  };
}

function summary(request) {
  const pieces = [request.city, formatDate(request.date), request.event_format, request.category, `до ${price(request.budget)}`];
  if (request.duration) pieces.push(`${request.duration} ${countWord(request.duration, ["час", "часа", "часов"])}`);
  if (request.language) pieces.push(request.language);
  return pieces.join(" · ");
}

function setStatus(text, loading = false) {
  statusLine.replaceChildren();
  if (!text) return;
  statusLine.append(el("span", loading ? "spinner" : "status-dot"), el("span", "", text));
  if (loading) statusLine.firstChild.setAttribute("aria-hidden", "true");
}

function setError(message, retry) {
  errorPanel.replaceChildren(el("p", "", message));
  if (retry) {
    const button = el("button", "", "Повторить");
    button.type = "button";
    button.addEventListener("click", retry);
    errorPanel.append(button);
  }
  errorPanel.hidden = false;
}

function showSkeletons() {
  resultContent.replaceChildren();
  for (let i = 0; i < 3; i += 1) {
    const card = el("div", "skeleton");
    card.setAttribute("aria-hidden", "true");
    for (let j = 0; j < 4; j += 1) card.append(el("div", "skeleton-line"));
    resultContent.append(card);
  }
}

function setSearchLoading(loading) {
  resultsPanel.setAttribute("aria-busy", String(loading));
  searchButton.disabled = loading;
  searchButton.firstElementChild.textContent = loading ? "Подбираем…" : "Подобрать подрядчиков";
  if (loading) {
    errorPanel.hidden = true;
    $("#result-count").hidden = true;
    $("#results-note").hidden = true;
    $("#diagnostics").hidden = true;
    setStatus("Проверяем условия и описания подрядчиков…", true);
    showSkeletons();
  }
}

function initials(name) {
  return String(name).split(/\s+/).filter(Boolean).slice(0, 2).map((part) => part[0]).join("").toLocaleUpperCase("ru-RU");
}

function contractorCard(card, index, request) {
  const article = el("article", "contractor-card");
  const headingId = `contractor-${index}`;
  article.setAttribute("aria-labelledby", headingId);
  article.dataset.profileId = card.id;
  const top = el("div", "card-top");
  const avatar = el("div", `avatar tone-${index % 3}`, initials(card.anon_name));
  avatar.setAttribute("aria-hidden", "true");
  const group = el("div", "card-title-group");
  const eyebrow = add(el("div", "card-eyebrow"), el("span", "rank-label", `${String(index + 1).padStart(2, "0")} / подборка`));
  const title = el("h3", "card-title", card.anon_name);
  title.id = headingId;
  add(group, eyebrow, title, el("p", "card-location", `${card.category || request.category} · ${card.city}`));
  const cost = el("div", "card-price");
  add(cost, el("span", "from", "от"), el("strong", "", price(card.price_from_kzt)), el("span", "price-caption", "начальная стоимость"));
  add(top, avatar, group, cost);
  const explanation = el("p", "card-explanation", card.explanation);
  const bottom = el("div", "card-bottom");
  const tags = el("div", "card-tags");
  tags.append(el("span", "tag", `✓ ${formatDate(card.available_on || request.date)} · свободен в календаре`));
  tags.append(el("span", card.synthetic ? "tag tag-amber" : "tag tag-neutral", card.synthetic ? "Синтетический профиль" : "Профиль из каталога"));
  if (card.price_imputed) tags.append(el("span", "tag tag-neutral", "Цена заполнена в датасете"));
  if (card.city_imputed) tags.append(el("span", "tag tag-neutral", "Город заполнен в датасете"));
  add(bottom, tags, el("span", "source-label", sourceName(card.explanation_source)));
  const details = el("details", "card-details");
  const detailsBody = el("div", "card-details-body");
  const languages = Array.isArray(card.languages) ? card.languages.join(", ") : "не указаны";
  const hours = card.max_hours == null ? "Лимит часов в каталоге не указан." : `Длительность по каталогу — до ${card.max_hours} ${countWord(card.max_hours, ["часа", "часов", "часов"])}.`;
  add(detailsBody, el("p", "", `Языки: ${languages}. ${hours}`));
  if (Array.isArray(card.categories) && card.categories.length > 1) detailsBody.append(el("p", "", `Категории: ${card.categories.join(", ")}.`));
  detailsBody.append(el("p", "", `Источник: ${card.origin === "team_extension" ? "расширение команды" : "исходный каталог"}. ID: ${card.id}.`));
  const evidence = card.explanation_evidence;
  if (evidence?.quote) {
    detailsBody.append(el("p", "", "Факт из описания профиля:"), el("blockquote", "evidence-quote", evidence.quote));
    detailsBody.append(el("p", "", card.synthetic ? "Источник — синтетическое описание для демонстрации." : "Это сведения из анкеты, а не независимая проверка опыта подрядчика."));
  }
  if (Array.isArray(card.warnings)) card.warnings.forEach((warning) => detailsBody.append(el("p", "warning-text", warning)));
  add(details, el("summary", "", "Данные и ограничения"), detailsBody);
  const action = el("button", "inquiry-button", card.synthetic ? "Посмотреть пример обращения" : "Обратиться в команду сервиса");
  action.type = "button";
  action.addEventListener("click", () => openInquiry(card, request));
  return add(article, top, explanation, bottom, details, action);
}

function renderGuidance(response) {
  const actions = response.assistant_suggestions || [];
  if (!actions.length) return;
  const section = el("section", "assistant-guidance");
  add(section, el("h3", "", "Что можно изменить"), el("p", "", "Проверили каталог: каждый вариант меняет одно условие. Выберите подходящий — пересчитаем подбор."));
  actions.forEach((action) => {
    const item = el("div", "guidance-item");
    const button = el("button", "guidance-button", action.label);
    button.type = "button";
    button.addEventListener("click", () => {
      applyRequest(action.request);
      document.querySelectorAll(".preset-button").forEach((preset) => preset.setAttribute("aria-pressed", "false"));
      statusLine.classList.remove("stale-note");
      runSearch();
    });
    add(item, button, el("p", "", action.explanation));
    section.append(item);
  });
  resultContent.append(section);
}

function emptyResult(response, request) {
  const empty = el("div", "empty-state");
  const noCategory = response.outcome === "NO_CATEGORY_IN_CITY";
  const stats = response.stats || {};
  const title = noCategory ? "В этом городе пока нет нужной категории" : "Подрядчики есть, но условия не совпали";
  const description = response.message || (noCategory ? `В каталоге нет категории «${request.category}» для города «${request.city}».` : "Никто из подрядчиков не прошёл все заданные условия.");
  let suggestion = "Попробуйте другую дату или скорректируйте условия в форме.";
  if (noCategory) suggestion = "Выберите другую категорию или город. Поиск всегда ограничен выбранным городом.";
  else if (stats.budget > 0 && stats.busy === 0) suggestion = "Начальная стоимость превышает бюджет. Увеличьте бюджет и повторите подбор.";
  else if (stats.busy > 0) suggestion = "Часть подрядчиков занята на эту дату. Попробуйте другую дату — её можно сравнить ниже.";
  const icon = el("div", "empty-icon", noCategory ? "⌕" : "↗");
  icon.setAttribute("aria-hidden", "true");
  return add(empty, icon, el("h3", "", title), el("p", "", description), el("p", "empty-suggestion", suggestion));
}

function buildDiagnostics(response) {
  const root = $("#diagnostic-content");
  root.replaceChildren();
  const funnel = el("div", "funnel");
  [[response.pool_count, "в городе и категории"], [response.eligible_count, "прошли условия"], [response.cards.length, "в подборке"]].forEach(([count, label]) => {
    funnel.append(add(el("div"), el("strong", "", count), el("span", "", label)));
  });
  const stats = el("div", "stats-grid");
  Object.entries(REASONS).forEach(([key, label]) => stats.append(add(el("div", "stat"), el("strong", "", response.stats?.[key] || 0), el("span", "", label))));
  add(root, funnel, el("p", "stats-heading", "Первая причина исключения"), stats, el("p", "diagnostic-note", "Каждый подрядчик учитывается один раз: сначала дата, затем бюджет, формат, длительность и язык. Отсутствие лимита часов не означает подтверждённую работу без ограничений."));
  const diagnostics = response.diagnostics || {};
  const meta = el("div", "diagnostic-meta");
  const semantic = isSemanticMode(diagnostics.rank_mode);
  add(meta,
    el("p", "", `Ранжирование: ${semantic ? "семантическое" : String(diagnostics.rank_mode || "не указан режим")}${diagnostics.embedding_model ? ` · ${diagnostics.embedding_model}` : ""}.`),
    el("p", "", "Формула: 0,5 × близость описания + 0,3 × цена / бюджет + вклад языка − 0,05 за синтетический профиль."),
    el("p", "", "Ценовой компонент даёт больший вклад более дорогим кандидатам внутри бюджета. Это правило ранжирования, а не оценка качества."),
    el("p", "", `Ответ: ${Number(response.elapsed_ms || 0).toLocaleString("ru-RU", { maximumFractionDigits: 0 })} мс.${diagnostics.cache_hit ? " Объяснения взяты из кеша." : ""}`));
  const technical = el("details", "technical-details");
  add(technical, el("summary", "", "Данные для проверки"), el("pre", "", JSON.stringify({
    outcome: response.outcome,
    pool_count: response.pool_count,
    eligible_count: response.eligible_count,
    stats: response.stats,
    diagnostics,
    ranking: response.cards.map((card) => ({ id: card.id, score: card.score, score_breakdown: card.score_breakdown, explanation_source: card.explanation_source })),
  }, null, 2)));
  add(root, meta, technical);
  $("#diagnostics").hidden = false;
}

function renderResponse(response, request) {
  if (!Array.isArray(response.cards) || !["SUCCESS", "NO_CATEGORY_IN_CITY", "ALL_FILTERED_OUT"].includes(response.outcome)) throw new ApiError("Сервис вернул неполный ответ. Повторите подбор.", 0);
  if (response.cards.length > 3) throw new ApiError("Сервис вернул больше трёх карточек. Повторите подбор.", 0);
  resultContent.replaceChildren();
  $("#query-summary").textContent = summary(request);
  $("#results-title").textContent = response.outcome === "SUCCESS" ? "Подходящие подрядчики" : "Результат подбора";
  if (response.outcome === "SUCCESS") {
    response.cards.forEach((card, index) => resultContent.append(contractorCard(card, index, request)));
    const count = response.cards.length;
    $("#result-count").textContent = `${count} ${countWord(count, ["кандидат", "кандидата", "кандидатов"])}`;
    $("#result-count").hidden = false;
    setStatus(`${response.eligible_count} из ${response.pool_count} прошли условия · показываем ${count}${response.eligible_count > count ? " первых" : ""}`);
    if (count < 3) resultContent.append(el("p", "result-explanation", response.message));
    $("#results-note").hidden = false;
  } else {
    resultContent.append(emptyResult(response, request));
    setStatus(response.outcome === "NO_CATEGORY_IN_CITY" ? "Пул города и категории пуст" : `Не прошли условия: ${response.pool_count} из ${response.pool_count}`);
  }
  renderGuidance(response);
  buildDiagnostics(response);
}

async function runSearch() {
  if (!metadata || !form.reportValidity()) return;
  let request;
  try { request = readRequest(); } catch (error) { setError(error.message); return; }
  const sequence = ++searchSequence;
  const startedRevision = formRevision;
  searchController?.abort();
  searchController = new AbortController();
  setSearchLoading(true);
  $("#query-summary").textContent = summary(request);
  try {
    const response = await callApi("/api/search", request, searchController);
    if (sequence !== searchSequence) return;
    renderResponse(response, request);
    lastRequest = request;
  } catch (error) {
    if (sequence !== searchSequence || error.name === "AbortError") return;
    resultContent.replaceChildren();
    setStatus("");
    setError(error.message, () => runSearch());
  } finally {
    if (sequence === searchSequence) {
      setSearchLoading(false);
      if (startedRevision !== formRevision) markResultsStale();
    }
  }
}

function markResultsStale() {
  if (!lastRequest || resultsPanel.getAttribute("aria-busy") === "true") return;
  setStatus("Условия изменились. Нажмите «Подобрать подрядчиков», чтобы обновить результат.");
  statusLine.classList.add("stale-note");
  document.querySelectorAll(".guidance-button, .inquiry-button").forEach((button) => { button.disabled = true; });
}

function markComparisonStale() {
  if (!$("#comparison-results").hidden || comparisonControllers.length) {
    comparisonSequence += 1;
    comparisonControllers.forEach((controller) => controller.abort());
    comparisonControllers = [];
    compareButton.disabled = !metadata;
    compareButton.firstChild.textContent = "Сравнить даты ";
    $("#comparison-status").textContent = "Условия изменились. Сравните даты заново для актуального результата.";
    $("#comparison-results").hidden = true;
  }
}

function compareColumn(response, request, otherResponse) {
  const otherIds = new Set(otherResponse.cards.map((card) => card.id));
  const otherRejections = otherResponse.diagnostics?.rejected_by_id;
  const column = el("section", "compare-column");
  add(column, el("h3", "", formatDate(request.date, true)), el("p", "compare-summary", `${response.eligible_count} из ${response.pool_count} прошли условия · ${response.cards.length} в подборке`));
  if (response.outcome !== "SUCCESS") {
    column.append(el("p", "compare-empty", response.message || "Нет кандидатов по заданным условиям."));
  } else {
    const list = el("ol", "compare-list");
    response.cards.forEach((card, index) => {
      const body = add(el("div", "compare-item-body"), el("span", "compare-name", card.anon_name), el("span", "compare-price", `от ${price(card.price_from_kzt)}${card.synthetic ? " · синтетический профиль" : ""}`), el("p", "compare-fact", card.explanation));
      const item = add(el("li", "compare-item"), el("span", "compare-rank", String(index + 1).padStart(2, "0")), body);
      if (!otherIds.has(card.id)) {
        const reason = otherRejections?.[card.id];
        const reasonLabels = {
          busy: "На другой дате занят в календаре",
          budget: "На другой дате исключён по бюджету",
          format: "На другой дате не подходит формат",
          duration: "На другой дате не подходит длительность",
          language: "На другой дате не подходит язык",
        };
        const label = reasonLabels[reason] || (otherRejections && typeof otherRejections === "object" && !Object.hasOwn(otherRejections, card.id)
          ? "На другой дате ниже топ-3"
          : "На другой дате вне подборки");
        body.append(el("span", "compare-badge", label));
      }
      list.append(item);
    });
    column.append(list);
  }
  const nonZero = Object.entries(REASONS).filter(([key]) => response.stats?.[key] > 0).map(([key, label]) => `${label.toLocaleLowerCase("ru-RU")}: ${response.stats[key]}`);
  if (nonZero.length) column.append(el("p", "diagnostic-note", `Первая причина исключения — ${nonZero.join(" · ")}.`));
  return column;
}

async function runComparison() {
  if (!metadata || !form.reportValidity() || !$("#comparison-form").reportValidity()) return;
  let base;
  try { base = readRequest(); } catch (error) { $("#comparison-status").textContent = error.message; return; }
  const dateA = $("#compare-date-a").value;
  const dateB = $("#compare-date-b").value;
  if (dateA === dateB) {
    $("#comparison-status").textContent = "Выберите две разные даты, чтобы сравнить доступность подрядчиков.";
    return;
  }
  const sequence = ++comparisonSequence;
  comparisonControllers.forEach((controller) => controller.abort());
  comparisonControllers = [new AbortController(), new AbortController()];
  compareButton.disabled = true;
  compareButton.firstChild.textContent = "Сравниваем… ";
  $("#comparison-status").textContent = "Проверяем одни и те же условия на двух датах…";
  $("#comparison-results").hidden = true;
  const requestA = { ...base, date: dateA };
  const requestB = { ...base, date: dateB };
  try {
    const [responseA, responseB] = await Promise.all([
      callApi("/api/search", requestA, comparisonControllers[0]),
      callApi("/api/search", requestB, comparisonControllers[1]),
    ]);
    if (sequence !== comparisonSequence) return;
    if (!Array.isArray(responseA.cards) || !Array.isArray(responseB.cards) || responseA.cards.length > 3 || responseB.cards.length > 3) throw new ApiError("Не удалось сопоставить ответы сервиса. Повторите сравнение.", 0);
    const idsA = new Set(responseA.cards.map((card) => card.id));
    const idsB = new Set(responseB.cards.map((card) => card.id));
    const shared = [...idsA].filter((id) => idsB.has(id)).length;
    const sameOrder = JSON.stringify([...idsA]) === JSON.stringify([...idsB]);
    let explanation = "На обеих датах нет подходящих кандидатов.";
    if (idsA.size || idsB.size) {
      if (sameOrder) explanation = "На этих датах состав и порядок подборки совпадают.";
      else if (shared === idsA.size && shared === idsB.size) explanation = "Состав совпадает, порядок кандидатов отличается.";
      else explanation = `Дата меняет состав подборки. Общих кандидатов: ${shared}; только на первой дате: ${idsA.size - shared}; только на второй: ${idsB.size - shared}.`;
    }
    const context = [base.city, base.category, base.event_format, `до ${price(base.budget)}`, ...(base.duration ? [`${base.duration} ч`] : []), ...(base.language ? [base.language] : [])].join(" · ");
    $("#comparison-status").textContent = `${context}. ${explanation}`;
    $("#comparison-results").replaceChildren(compareColumn(responseA, requestA, responseB), compareColumn(responseB, requestB, responseA));
    $("#comparison-results").hidden = false;
  } catch (error) {
    if (sequence !== comparisonSequence || error.name === "AbortError") return;
    comparisonControllers.forEach((controller) => controller.abort());
    $("#comparison-status").textContent = error.message;
  } finally {
    if (sequence === comparisonSequence) {
      compareButton.disabled = false;
      compareButton.firstChild.textContent = "Сравнить даты ";
      comparisonControllers = [];
    }
  }
}

function buildPresets(presets) {
  $("#presets").replaceChildren();
  presets.forEach((preset) => {
    const button = el("button", "preset-button", preset.label);
    button.type = "button";
    button.setAttribute("aria-pressed", "false");
    if (preset.description) button.title = preset.description;
    button.addEventListener("click", () => {
      applyRequest(preset.request);
      document.querySelectorAll(".preset-button").forEach((item) => item.setAttribute("aria-pressed", String(item === button)));
      statusLine.classList.remove("stale-note");
      runSearch();
    });
    $("#presets").append(button);
  });
}

async function initialize() {
  errorPanel.hidden = true;
  try {
    const meta = await callApi("/api/meta");
    for (const key of ["cities", "categories", "event_formats", "languages"]) {
      if (!Array.isArray(meta[key]) || !meta[key].length) throw new ApiError("Каталог ещё не готов. Повторите загрузку.", 503);
    }
    metadata = meta;
    setOptions($("#city"), meta.cities);
    setOptions($("#category"), meta.categories);
    setOptions($("#event_format"), meta.event_formats);
    setOptions($("#language"), meta.languages, "Любой");
    [$("#date"), $("#compare-date-a"), $("#compare-date-b")].forEach((input) => {
      input.min = meta.date_min;
      input.max = meta.date_max;
    });
    $("#date-hint").textContent = `Каталог: ${formatDate(meta.date_min)} — ${formatDate(meta.date_max, true)}`;
    $("#catalog-total").textContent = meta.catalog?.total ?? "—";
    $("#catalog-cities").textContent = meta.cities.length;
    $("#catalog-categories").textContent = meta.categories.length;
    $("#runtime-label").textContent = `${isSemanticMode(meta.runtime?.rank_mode) ? "Семантический подбор" : "Базовое ранжирование"} · ${runtimeName(meta.runtime?.explanation_mode)}`;
    document.querySelectorAll("#search-form input, #search-form select, #search-button, #comparison-form input, #compare-button").forEach((control) => { control.disabled = false; });
    const presets = Array.isArray(meta.presets) ? meta.presets : [];
    buildPresets(presets);
    const defaultPreset = presets.find((preset) => preset.request?.city === "Алматы" && preset.request?.category === "Ведущий" && preset.request?.date === "2026-10-04") || presets[0];
    const initial = defaultPreset?.request || { city: meta.cities[0], category: meta.categories[0], date: meta.date_min, event_format: meta.event_formats[0], budget: 2000000, duration: null, language: null };
    applyRequest(initial);
    if (defaultPreset) [...$("#presets").children][presets.indexOf(defaultPreset)]?.setAttribute("aria-pressed", "true");
    $("#compare-date-a").value = meta.comparison?.date_a || initial.date;
    $("#compare-date-b").value = meta.comparison?.date_b || meta.date_max;
    await runSearch();
  } catch (error) {
    resultsPanel.setAttribute("aria-busy", "false");
    $("#query-summary").textContent = "Не удалось загрузить каталог";
    $("#presets").replaceChildren(el("span", "muted", "Сценарии появятся после загрузки каталога."));
    resultContent.replaceChildren();
    $("#runtime-label").textContent = "Нет соединения с сервисом";
    setError(error.message, initialize);
  }
}

form.addEventListener("submit", (event) => { event.preventDefault(); statusLine.classList.remove("stale-note"); runSearch(); });
$("#inquiry-close").addEventListener("click", closeInquiry);
$("#inquiry-dialog").addEventListener("cancel", (event) => { event.preventDefault(); closeInquiry(); });
$("#inquiry-form").addEventListener("submit", submitInquiry);
$("#inquiry-download").addEventListener("click", () => {
  if (!inquirySelection) return;
  const url = URL.createObjectURL(new Blob([inquirySelection.brief], { type: "text/plain;charset=utf-8" }));
  const link = el("a"); link.href = url; link.download = `event-brief-${inquirySelection.card.id}.txt`;
  document.body.append(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000);
});
form.addEventListener("input", () => {
  formRevision += 1;
  document.querySelectorAll(".preset-button").forEach((button) => button.setAttribute("aria-pressed", "false"));
  markResultsStale();
  markComparisonStale();
});
$("#comparison-form").addEventListener("submit", (event) => { event.preventDefault(); runComparison(); });
$("#compare-date-a").addEventListener("input", markComparisonStale);
$("#compare-date-b").addEventListener("input", markComparisonStale);
initialize();
