import { escapeHtml, renderMarkdown } from "/assets/markdown.js";

const PENDING_STATUSES = new Set(["queued", "running", "retrying"]);

const elements = {
  sidebar: byId("sidebar"),
  sidebarOpen: byId("sidebar-open"),
  sidebarClose: byId("sidebar-close"),
  sidebarScrim: byId("sidebar-scrim"),
  newChat: byId("new-chat-button"),
  welcomeNewChat: byId("welcome-new-chat"),
  welcomeAddModel: byId("welcome-add-model"),
  search: byId("conversation-search"),
  conversationList: byId("conversation-list"),
  conversationCount: byId("conversation-count"),
  profileCount: byId("profile-count"),
  manageModelsSidebar: byId("manage-models-sidebar"),
  manageModelsTop: byId("manage-models-top"),
  themeToggle: byId("theme-toggle"),
  themeIcon: byId("theme-icon"),
  themeLabel: byId("theme-label"),
  chatTitle: byId("chat-title"),
  modelSelect: byId("model-select"),
  welcome: byId("welcome-state"),
  chatView: byId("chat-view"),
  messages: byId("messages"),
  jumpBottom: byId("jump-bottom"),
  composer: byId("composer-form"),
  messageInput: byId("message-input"),
  composerStatus: byId("composer-status"),
  sendButton: byId("send-button"),
  stopButton: byId("stop-button"),
  modelDialog: byId("model-dialog"),
  modelDialogClose: byId("model-dialog-close"),
  addProfile: byId("add-profile-button"),
  profileList: byId("profile-list"),
  profileForm: byId("profile-form"),
  profileFormTitle: byId("profile-form-title"),
  formatChip: byId("format-chip"),
  tokenInput: byId("profile-token"),
  tokenVisibility: byId("token-visibility"),
  tokenHelp: byId("token-help"),
  connectionResult: byId("connection-result"),
  testProfile: byId("test-profile-button"),
  deleteProfile: byId("delete-profile-button"),
  toastRegion: byId("toast-region"),
};

const state = {
  profiles: [],
  conversations: [],
  current: null,
  editingProfileId: null,
  editingMessageId: null,
  searchTimer: null,
  pollTimer: null,
  busy: false,
};

class ApiError extends Error {
  constructor(status, code, message) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

initialize();

async function initialize() {
  attachEvents();
  applyInitialTheme();
  try {
    await Promise.all([loadProfiles(), loadConversations()]);
    const remembered = localStorage.getItem("datalab.currentConversation");
    const preferred = state.conversations.find((item) => item.id === remembered)?.id;
    const initial = preferred || state.conversations[0]?.id;
    if (initial) {
      await openConversation(initial, { scroll: true, closeSidebar: false });
    } else {
      renderWelcome();
    }
  } catch (error) {
    renderWelcome();
    reportError(error);
  }
}

function attachEvents() {
  elements.newChat.addEventListener("click", () => guard(createConversation));
  elements.welcomeNewChat.addEventListener("click", () => guard(createConversation));
  elements.welcomeAddModel.addEventListener("click", () => openModelDialog());
  elements.manageModelsSidebar.addEventListener("click", () => openModelDialog());
  elements.manageModelsTop.addEventListener("click", () => openModelDialog());
  elements.modelDialogClose.addEventListener("click", closeModelDialog);
  elements.addProfile.addEventListener("click", resetProfileForm);
  elements.profileForm.addEventListener("submit", (event) => {
    event.preventDefault();
    guard(saveProfile);
  });
  elements.testProfile.addEventListener("click", () => guard(testCurrentProfile));
  elements.deleteProfile.addEventListener("click", () => guard(deleteCurrentProfile));
  elements.tokenVisibility.addEventListener("click", toggleTokenVisibility);
  elements.profileForm.elements.format.addEventListener("change", updateFormatChip);
  elements.profileList.addEventListener("click", handleProfileListClick);
  elements.conversationList.addEventListener("click", handleConversationListClick);
  elements.composer.addEventListener("submit", (event) => {
    event.preventDefault();
    guard(sendMessage);
  });
  elements.messageInput.addEventListener("input", resizeComposer);
  elements.messageInput.addEventListener("keydown", handleComposerKeydown);
  elements.messages.addEventListener("click", handleMessageAction);
  elements.messages.addEventListener("scroll", updateJumpButton, { passive: true });
  elements.jumpBottom.addEventListener("click", () => scrollToBottom(true));
  elements.stopButton.addEventListener("click", () => guard(cancelGeneration));
  elements.modelSelect.addEventListener("change", () => guard(changeActiveProfile));
  elements.chatTitle.addEventListener("click", () => guard(renameCurrentConversation));
  elements.search.addEventListener("input", scheduleSearch);
  elements.sidebarOpen.addEventListener("click", openSidebar);
  elements.sidebarClose.addEventListener("click", closeSidebar);
  elements.sidebarScrim.addEventListener("click", closeSidebar);
  elements.themeToggle.addEventListener("click", toggleTheme);
  elements.modelDialog.addEventListener("click", (event) => {
    if (event.target === elements.modelDialog) closeModelDialog();
  });
  document.addEventListener("keydown", handleGlobalKeydown);
}

async function api(path, options = {}) {
  const method = options.method || "GET";
  const request = { method, headers: { Accept: "application/json" } };
  if (!new Set(["GET", "HEAD"]).has(method)) {
    request.headers["Content-Type"] = "application/json";
    request.headers["X-DataLab-UI"] = "browser";
    request.body = JSON.stringify(options.body ?? {});
  }
  try {
    const response = await fetch(path, request);
    const contentType = response.headers.get("Content-Type") || "";
    const payload = contentType.includes("application/json") ? await response.json() : null;
    if (!response.ok) {
      throw new ApiError(
        response.status,
        payload?.error?.code || "request_failed",
        payload?.error?.message || "Запрос не выполнен.",
      );
    }
    return payload;
  } catch (error) {
    if (error instanceof ApiError) throw error;
    throw new ApiError(0, "connection_error", "Локальный сервис не отвечает.");
  }
}

async function loadProfiles() {
  state.profiles = await api("/api/profiles");
  renderProfileOptions();
  renderProfileList();
}

async function loadConversations() {
  const query = elements.search.value.trim();
  const suffix = query ? `?query=${encodeURIComponent(query)}` : "";
  state.conversations = await api(`/api/conversations${suffix}`);
  renderConversationList();
}

async function openConversation(conversationId, options = {}) {
  const { scroll = false, closeSidebar: shouldClose = true } = options;
  try {
    const conversation = await api(`/api/conversations/${conversationId}`);
    state.current = conversation;
    localStorage.setItem("datalab.currentConversation", conversation.id);
    renderConversation({ scroll });
    renderConversationList();
    scheduleGenerationPoll();
    if (shouldClose) closeSidebar();
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) {
      localStorage.removeItem("datalab.currentConversation");
      state.current = null;
      await loadConversations();
      renderWelcome();
      return;
    }
    throw error;
  }
}

async function createConversation() {
  if (!state.profiles.length) {
    openModelDialog();
    toast("Сначала добавьте профиль модели.", "error");
    return;
  }
  const profileId = elements.modelSelect.value || state.profiles[0].id;
  const conversation = await api("/api/conversations", {
    method: "POST",
    body: { profile_id: profileId },
  });
  elements.search.value = "";
  await loadConversations();
  await openConversation(conversation.id, { scroll: true });
  elements.messageInput.focus();
}

function renderWelcome() {
  clearTimeout(state.pollTimer);
  state.pollTimer = null;
  state.current = null;
  elements.welcome.hidden = false;
  elements.chatView.hidden = true;
  elements.chatTitle.textContent = "Новый чат";
  elements.chatTitle.disabled = true;
  renderProfileOptions();
  updateComposerState();
}

function renderConversation({ scroll = false } = {}) {
  if (!state.current) {
    renderWelcome();
    return;
  }
  elements.welcome.hidden = true;
  elements.chatView.hidden = false;
  elements.chatTitle.disabled = false;
  elements.chatTitle.textContent = state.current.title;
  renderProfileOptions();

  const messages = state.current.messages || [];
  const body = messages.length
    ? messages.map(renderMessage).join("")
    : renderEmptyConversation();
  const generation = renderGeneration(state.current.active_generation);
  elements.messages.innerHTML = `<div class="messages-inner">${body}${generation}</div>`;
  updateComposerState();
  if (scroll) requestAnimationFrame(() => scrollToBottom(false));
}

function renderEmptyConversation() {
  return `<div class="empty-chat">
    <p class="eyebrow">НОВЫЙ ДИАЛОГ</p>
    <h2>С чего начнём?</h2>
    <p>Выбранный профиль используется только для следующего ответа.</p>
    <div class="prompt-grid">
      <button type="button" data-action="use-prompt" data-prompt="Объясни ключевые факторы кредитного риска простыми словами">Объяснить термин</button>
      <button type="button" data-action="use-prompt" data-prompt="Помоги структурировать анализ рисков по шагам">Составить план анализа</button>
      <button type="button" data-action="use-prompt" data-prompt="Проверь логику следующего вывода и укажи слабые места: ">Проверить рассуждение</button>
    </div>
  </div>`;
}

function renderMessage(message) {
  const user = message.role === "user";
  const snapshot = message.model_snapshot;
  const modelBadge = snapshot
    ? `<span class="model-snapshot" title="${escapeHtml(snapshot.format)} · ${escapeHtml(snapshot.model_id)}">${escapeHtml(snapshot.display_name)}</span>`
    : "";
  const branch = renderVariantControl(message);
  const actions = user
    ? `${branch}<button class="message-action" type="button" data-action="copy-message" data-message-id="${message.id}">Копировать</button><button class="message-action" type="button" data-action="edit-message" data-message-id="${message.id}">Изменить</button>`
    : `${branch}<button class="message-action" type="button" data-action="copy-message" data-message-id="${message.id}">Копировать</button><button class="message-action" type="button" data-action="regenerate" data-message-id="${message.id}">Регенерировать</button>`;

  return `<article class="message ${user ? "user" : "assistant"}" data-message="${message.id}">
    <div class="message-avatar" aria-hidden="true">${user ? "ВЫ" : "AI"}</div>
    <div class="message-card">
      <div class="message-meta"><span class="message-role">${user ? "Вы" : "Ассистент"}</span>${modelBadge}</div>
      <div class="message-content">${renderMarkdown(message.content)}</div>
      <div class="message-actions">${actions}</div>
    </div>
  </article>`;
}

function renderVariantControl(message) {
  if (!message.variant_count || message.variant_count < 2) return "";
  return `<span class="variant-control">
    <button type="button" data-action="variant-prev" data-message-id="${message.id}" aria-label="Предыдущий вариант" ${message.variant_index <= 1 ? "disabled" : ""}>‹</button>
    <span>${message.variant_index}/${message.variant_count}</span>
    <button type="button" data-action="variant-next" data-message-id="${message.id}" aria-label="Следующий вариант" ${message.variant_index >= message.variant_count ? "disabled" : ""}>›</button>
  </span>`;
}

function renderGeneration(generation) {
  if (!generation) return "";
  if (PENDING_STATUSES.has(generation.status)) {
    const retryText = generation.status === "retrying" ? "Повторяем запрос после временной ошибки" : "Ожидаем полный ответ модели";
    return `<div class="generation-card">
      <span class="spinner" aria-hidden="true"></span>
      <div class="generation-copy"><strong>${escapeHtml(retryText)}</strong><span>Попытка ${Math.max(1, generation.attempts)} из 3 · общий тайм-аут до 10 минут</span></div>
    </div>`;
  }
  const labels = {
    failed: "Генерация не выполнена",
    interrupted: "Генерация прервана перезапуском",
    cancelled: "Генерация отменена",
  };
  const label = labels[generation.status] || "Генерация завершена с ошибкой";
  return `<div class="generation-card error">
    <div class="generation-copy"><strong>${escapeHtml(label)}</strong><span>${escapeHtml(generation.error_message || "Можно безопасно повторить запрос.")}</span></div>
    <button class="button button-secondary compact" type="button" data-action="retry-generation" data-generation-id="${generation.id}">Повторить</button>
  </div>`;
}

function renderConversationList() {
  elements.conversationCount.textContent = String(state.conversations.length);
  if (!state.conversations.length) {
    elements.conversationList.innerHTML = `<div class="empty-list">${elements.search.value.trim() ? "Ничего не найдено" : "История пока пуста"}</div>`;
    return;
  }
  elements.conversationList.innerHTML = state.conversations
    .map((conversation) => {
      const active = conversation.id === state.current?.id;
      return `<div class="conversation-row ${active ? "active" : ""}" data-conversation-row="${conversation.id}">
        <button class="conversation-item" type="button" data-action="open-conversation" data-conversation-id="${conversation.id}">
          <span class="conversation-title-text">${escapeHtml(conversation.title)}</span>
          <span class="conversation-time">${escapeHtml(relativeTime(conversation.updated_at))}</span>
        </button>
        <span class="conversation-tools">
          <button class="mini-button" type="button" data-action="rename-conversation" data-conversation-id="${conversation.id}" aria-label="Переименовать">✎</button>
          <button class="mini-button" type="button" data-action="delete-conversation" data-conversation-id="${conversation.id}" aria-label="Удалить">×</button>
        </span>
      </div>`;
    })
    .join("");
}

function renderProfileOptions() {
  elements.profileCount.textContent = String(state.profiles.length);
  if (!state.profiles.length) {
    elements.modelSelect.innerHTML = '<option value="">Добавьте профиль</option>';
    elements.modelSelect.disabled = true;
    return;
  }
  elements.modelSelect.disabled = false;
  elements.modelSelect.innerHTML = state.profiles
    .map((profile) => `<option value="${profile.id}">${escapeHtml(profile.display_name)}</option>`)
    .join("");
  const selected = state.current?.active_profile_id;
  if (selected && state.profiles.some((profile) => profile.id === selected)) {
    elements.modelSelect.value = selected;
  } else {
    elements.modelSelect.value = state.profiles[0].id;
  }
}

function renderProfileList() {
  if (!state.profiles.length) {
    elements.profileList.innerHTML = '<div class="empty-list">Нет сохранённых профилей</div>';
    return;
  }
  elements.profileList.innerHTML = state.profiles
    .map((profile) => `<button class="profile-list-item ${profile.id === state.editingProfileId ? "active" : ""}" type="button" data-profile-id="${profile.id}">
      <span class="profile-format-icon">${profile.format === "gigachat" ? "GC" : "OA"}</span>
      <span class="profile-list-copy"><strong>${escapeHtml(profile.display_name)}</strong><span>${escapeHtml(profile.model_id)}</span></span>
    </button>`)
    .join("");
}

function updateComposerState() {
  const generation = state.current?.active_generation;
  const pending = generation && PENDING_STATUSES.has(generation.status);
  elements.stopButton.hidden = !pending;
  elements.sendButton.disabled = Boolean(pending || state.busy || !state.profiles.length);
  elements.messageInput.disabled = Boolean(pending);
  elements.modelSelect.disabled = !state.profiles.length;
  if (pending) {
    elements.composerStatus.textContent = generation.status === "retrying" ? "Временная ошибка — выполняется повтор" : "Запрос выполняется без streaming";
  } else if (!state.profiles.length) {
    elements.composerStatus.textContent = "Добавьте профиль модели, чтобы отправить сообщение";
  } else {
    elements.composerStatus.textContent = "Enter — отправить · Shift+Enter — новая строка";
  }
}

async function sendMessage() {
  const content = elements.messageInput.value.trim();
  if (!content) return;
  if (!state.profiles.length) {
    openModelDialog();
    throw new ApiError(400, "profile_required", "Сначала добавьте профиль модели.");
  }
  state.busy = true;
  updateComposerState();
  try {
    if (!state.current) await createConversation();
    if (!state.current) return;
    await api(`/api/conversations/${state.current.id}/messages`, {
      method: "POST",
      body: { content, profile_id: elements.modelSelect.value },
    });
    elements.messageInput.value = "";
    resizeComposer();
    await openConversation(state.current.id, { scroll: true, closeSidebar: false });
    await loadConversations();
  } finally {
    state.busy = false;
    updateComposerState();
  }
}

async function editMessage(messageId, content) {
  await api(`/api/messages/${messageId}/edit`, {
    method: "POST",
    body: { content, profile_id: elements.modelSelect.value },
  });
  state.editingMessageId = null;
  await openConversation(state.current.id, { scroll: true, closeSidebar: false });
  await loadConversations();
}

async function regenerate(messageId) {
  await api(`/api/messages/${messageId}/regenerate`, {
    method: "POST",
    body: { profile_id: elements.modelSelect.value },
  });
  await openConversation(state.current.id, { scroll: true, closeSidebar: false });
}

async function retryGeneration(generationId) {
  await api(`/api/generations/${generationId}/retry`, {
    method: "POST",
    body: { profile_id: elements.modelSelect.value },
  });
  await openConversation(state.current.id, { scroll: true, closeSidebar: false });
}

async function cancelGeneration() {
  const generation = state.current?.active_generation;
  if (!generation || !PENDING_STATUSES.has(generation.status)) return;
  await api(`/api/generations/${generation.id}/cancel`, { method: "POST" });
  await openConversation(state.current.id, { scroll: true, closeSidebar: false });
  toast("Генерация отменена.");
}

async function selectVariant(message, direction) {
  const currentIndex = message.variant_index - 1;
  const targetIndex = currentIndex + direction;
  const targetId = message.variant_ids?.[targetIndex];
  if (!targetId) return;
  await api(`/api/conversations/${state.current.id}/select`, {
    method: "POST",
    body: { message_id: targetId },
  });
  await openConversation(state.current.id, { scroll: false, closeSidebar: false });
}

function scheduleGenerationPoll() {
  clearTimeout(state.pollTimer);
  state.pollTimer = null;
  const generation = state.current?.active_generation;
  if (!generation || !PENDING_STATUSES.has(generation.status)) return;
  state.pollTimer = setTimeout(pollGeneration, 800);
}

async function pollGeneration() {
  const conversationId = state.current?.id;
  if (!conversationId) return;
  try {
    const conversation = await api(`/api/conversations/${conversationId}`);
    if (state.current?.id !== conversationId) return;
    const wasPending = PENDING_STATUSES.has(state.current?.active_generation?.status);
    state.current = conversation;
    const stillPending = PENDING_STATUSES.has(conversation.active_generation?.status);
    renderConversation({ scroll: wasPending && !stillPending });
    if (!stillPending) await loadConversations();
  } catch (error) {
    if (!(error instanceof ApiError && error.code === "connection_error")) reportError(error);
  } finally {
    scheduleGenerationPoll();
  }
}

function handleMessageAction(event) {
  const button = event.target.closest("button[data-action]");
  if (!button) return;
  const action = button.dataset.action;
  const message = state.current?.messages.find((item) => item.id === button.dataset.messageId);
  if (action === "copy-code") {
    const code = button.closest(".code-block")?.querySelector("code")?.textContent || "";
    guard(() => copyText(code, "Код скопирован."));
  } else if (action === "copy-message" && message) {
    guard(() => copyText(message.content, "Ответ скопирован."));
  } else if (action === "edit-message" && message) {
    startInlineEdit(message);
  } else if (action === "cancel-edit") {
    state.editingMessageId = null;
    renderConversation();
  } else if (action === "save-edit" && message) {
    const textarea = button.closest(".inline-editor")?.querySelector("textarea");
    if (textarea?.value.trim()) guard(() => editMessage(message.id, textarea.value.trim()));
  } else if (action === "regenerate" && message) {
    guard(() => regenerate(message.id));
  } else if (action === "variant-prev" && message) {
    guard(() => selectVariant(message, -1));
  } else if (action === "variant-next" && message) {
    guard(() => selectVariant(message, 1));
  } else if (action === "retry-generation") {
    guard(() => retryGeneration(button.dataset.generationId));
  } else if (action === "use-prompt") {
    elements.messageInput.value = button.dataset.prompt || "";
    resizeComposer();
    elements.messageInput.focus();
  }
}

function startInlineEdit(message) {
  state.editingMessageId = message.id;
  const article = elements.messages.querySelector(`[data-message="${message.id}"]`);
  const card = article?.querySelector(".message-card");
  if (!card) return;
  const content = card.querySelector(".message-content");
  const actions = card.querySelector(".message-actions");
  if (content) content.hidden = true;
  if (actions) actions.hidden = true;
  const editor = document.createElement("div");
  editor.className = "inline-editor";
  const textarea = document.createElement("textarea");
  textarea.value = message.content;
  textarea.setAttribute("aria-label", "Изменённое сообщение");
  const controls = document.createElement("div");
  controls.className = "inline-editor-actions";
  controls.innerHTML = `<button class="button button-secondary compact" type="button" data-action="cancel-edit">Отмена</button><button class="button button-primary compact" type="button" data-action="save-edit" data-message-id="${message.id}">Сохранить и отправить</button>`;
  editor.append(textarea, controls);
  card.append(editor);
  textarea.focus();
  textarea.setSelectionRange(textarea.value.length, textarea.value.length);
}

function handleConversationListClick(event) {
  const button = event.target.closest("button[data-action]");
  if (!button) return;
  const conversationId = button.dataset.conversationId;
  if (button.dataset.action === "open-conversation") {
    guard(() => openConversation(conversationId, { scroll: false }));
  } else if (button.dataset.action === "rename-conversation") {
    guard(() => renameConversation(conversationId));
  } else if (button.dataset.action === "delete-conversation") {
    guard(() => deleteConversation(conversationId));
  }
}

async function renameCurrentConversation() {
  if (state.current) await renameConversation(state.current.id);
}

async function renameConversation(conversationId) {
  const conversation = state.conversations.find((item) => item.id === conversationId) || state.current;
  const title = window.prompt("Название диалога", conversation?.title || "");
  if (!title?.trim()) return;
  await api(`/api/conversations/${conversationId}`, {
    method: "PATCH",
    body: { title: title.trim() },
  });
  await loadConversations();
  if (state.current?.id === conversationId) await openConversation(conversationId, { closeSidebar: false });
}

async function deleteConversation(conversationId) {
  const conversation = state.conversations.find((item) => item.id === conversationId);
  if (!window.confirm(`Удалить диалог «${conversation?.title || "Без названия"}»? Это действие необратимо.`)) return;
  await api(`/api/conversations/${conversationId}`, { method: "DELETE" });
  const wasCurrent = state.current?.id === conversationId;
  if (wasCurrent) {
    state.current = null;
    localStorage.removeItem("datalab.currentConversation");
  }
  await loadConversations();
  if (wasCurrent && state.conversations[0]) {
    await openConversation(state.conversations[0].id, { scroll: false });
  } else if (wasCurrent) {
    renderWelcome();
  }
  toast("Диалог удалён.");
}

async function changeActiveProfile() {
  if (!state.current || !elements.modelSelect.value) return;
  await api(`/api/conversations/${state.current.id}`, {
    method: "PATCH",
    body: { profile_id: elements.modelSelect.value },
  });
  state.current.active_profile_id = elements.modelSelect.value;
  renderConversation();
}

function openModelDialog(profileId = null) {
  closeSidebar();
  if (profileId) {
    selectProfileForEdit(profileId);
  } else if (state.profiles.length) {
    selectProfileForEdit(state.editingProfileId || state.profiles[0].id);
  } else {
    resetProfileForm();
  }
  if (!elements.modelDialog.open) elements.modelDialog.showModal();
}

function closeModelDialog() {
  if (elements.modelDialog.open) elements.modelDialog.close();
}

function resetProfileForm() {
  state.editingProfileId = null;
  elements.profileForm.reset();
  elements.profileForm.elements.format.value = "gigachat";
  elements.tokenInput.required = true;
  elements.tokenInput.type = "password";
  elements.tokenVisibility.textContent = "Показать";
  elements.profileFormTitle.textContent = "Новый профиль";
  elements.tokenHelp.textContent = "Токен хранится локально и никогда не отображается повторно.";
  elements.testProfile.disabled = false;
  elements.deleteProfile.hidden = true;
  elements.connectionResult.hidden = true;
  updateFormatChip();
  renderProfileList();
  elements.profileForm.elements.display_name.focus();
}

function selectProfileForEdit(profileId) {
  const profile = state.profiles.find((item) => item.id === profileId);
  if (!profile) {
    resetProfileForm();
    return;
  }
  state.editingProfileId = profile.id;
  elements.profileForm.elements.display_name.value = profile.display_name;
  elements.profileForm.elements.format.value = profile.format;
  elements.profileForm.elements.base_url.value = profile.base_url;
  elements.profileForm.elements.token.value = "";
  elements.profileForm.elements.model_id.value = profile.model_id;
  elements.tokenInput.required = false;
  elements.tokenHelp.textContent = "Оставьте поле пустым, чтобы сохранить действующий токен.";
  elements.profileFormTitle.textContent = profile.display_name;
  elements.testProfile.disabled = false;
  elements.deleteProfile.hidden = false;
  elements.connectionResult.hidden = true;
  updateFormatChip();
  renderProfileList();
}

function handleProfileListClick(event) {
  const button = event.target.closest("button[data-profile-id]");
  if (button) selectProfileForEdit(button.dataset.profileId);
}

async function saveProfile() {
  const values = Object.fromEntries(new FormData(elements.profileForm));
  const creating = !state.editingProfileId;
  if (creating && !String(values.token || "").trim()) {
    throw new ApiError(400, "validation_error", "Введите токен профиля.");
  }
  const path = creating ? "/api/profiles" : `/api/profiles/${state.editingProfileId}`;
  const profile = await api(path, { method: creating ? "POST" : "PUT", body: values });
  state.editingProfileId = profile.id;
  await loadProfiles();
  selectProfileForEdit(profile.id);
  if (state.current && !state.current.active_profile_id) {
    await changeProfileTo(profile.id);
  }
  toast(creating ? "Профиль добавлен." : "Профиль обновлён.");
}

async function testCurrentProfile() {
  if (!elements.profileForm.reportValidity()) return;
  const values = Object.fromEntries(new FormData(elements.profileForm));
  if (!state.editingProfileId && !String(values.token || "").trim()) {
    throw new ApiError(400, "validation_error", "Введите токен профиля.");
  }
  setConnectionResult("Проверяем подключение…", false);
  elements.testProfile.disabled = true;
  try {
    const result = await api("/api/profiles/test", {
      method: "POST",
      body: { ...values, profile_id: state.editingProfileId, timeout_seconds: 30 },
    });
    setConnectionResult(`Подключение работает · ${result.latency_ms} мс · ${result.preview}`, false);
  } catch (error) {
    setConnectionResult(error.message || "Проверка не выполнена.", true);
  } finally {
    elements.testProfile.disabled = false;
  }
}

async function deleteCurrentProfile() {
  const profile = state.profiles.find((item) => item.id === state.editingProfileId);
  if (!profile) return;
  if (!window.confirm(`Удалить профиль «${profile.display_name}»? История ответов останется доступной.`)) return;
  await api(`/api/profiles/${profile.id}`, { method: "DELETE" });
  state.editingProfileId = null;
  await loadProfiles();
  if (state.current) await openConversation(state.current.id, { closeSidebar: false });
  if (state.profiles.length) selectProfileForEdit(state.profiles[0].id);
  else resetProfileForm();
  toast("Профиль удалён.");
}

async function changeProfileTo(profileId) {
  if (!state.current) return;
  await api(`/api/conversations/${state.current.id}`, {
    method: "PATCH",
    body: { profile_id: profileId },
  });
  await openConversation(state.current.id, { closeSidebar: false });
}

function setConnectionResult(message, isError) {
  elements.connectionResult.textContent = message;
  elements.connectionResult.classList.toggle("error", isError);
  elements.connectionResult.hidden = false;
}

function updateFormatChip() {
  elements.formatChip.textContent = elements.profileForm.elements.format.value === "gigachat" ? "GigaChat" : "OpenAI-compatible";
}

function toggleTokenVisibility() {
  const showing = elements.tokenInput.type === "text";
  elements.tokenInput.type = showing ? "password" : "text";
  elements.tokenVisibility.textContent = showing ? "Показать" : "Скрыть";
}

function scheduleSearch() {
  clearTimeout(state.searchTimer);
  state.searchTimer = setTimeout(() => guard(loadConversations), 220);
}

function handleComposerKeydown(event) {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    if (!elements.sendButton.disabled) guard(sendMessage);
  }
}

function handleGlobalKeydown(event) {
  if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
    event.preventDefault();
    guard(createConversation);
  }
  if (event.key === "Escape") closeSidebar();
}

function resizeComposer() {
  elements.messageInput.style.height = "auto";
  elements.messageInput.style.height = `${Math.min(elements.messageInput.scrollHeight, 180)}px`;
}

function updateJumpButton() {
  const distance = elements.messages.scrollHeight - elements.messages.scrollTop - elements.messages.clientHeight;
  elements.jumpBottom.hidden = distance < 180;
}

function scrollToBottom(smooth) {
  elements.messages.scrollTo({ top: elements.messages.scrollHeight, behavior: smooth ? "smooth" : "auto" });
  elements.jumpBottom.hidden = true;
}

function openSidebar() {
  document.body.classList.add("sidebar-open");
}

function closeSidebar() {
  document.body.classList.remove("sidebar-open");
}

function applyInitialTheme() {
  const saved = localStorage.getItem("datalab.theme");
  const systemDark = window.matchMedia?.("(prefers-color-scheme: dark)").matches;
  applyTheme(saved || (systemDark ? "dark" : "light"));
}

function toggleTheme() {
  applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark");
}

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  localStorage.setItem("datalab.theme", theme);
  elements.themeIcon.textContent = theme === "dark" ? "☾" : "☀";
  elements.themeLabel.textContent = theme === "dark" ? "Тёмная тема" : "Светлая тема";
}

async function copyText(text, successMessage) {
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    const helper = document.createElement("textarea");
    helper.value = text;
    helper.setAttribute("readonly", "");
    helper.className = "clipboard-helper";
    document.body.append(helper);
    helper.select();
    document.execCommand("copy");
    helper.remove();
  }
  toast(successMessage);
}

function relativeTime(value) {
  try {
    const date = new Date(value);
    const seconds = Math.round((date.getTime() - Date.now()) / 1000);
    const formatter = new Intl.RelativeTimeFormat("ru", { numeric: "auto" });
    if (Math.abs(seconds) < 60) return formatter.format(seconds, "second");
    const minutes = Math.round(seconds / 60);
    if (Math.abs(minutes) < 60) return formatter.format(minutes, "minute");
    const hours = Math.round(minutes / 60);
    if (Math.abs(hours) < 24) return formatter.format(hours, "hour");
    const days = Math.round(hours / 24);
    return formatter.format(days, "day");
  } catch {
    return "";
  }
}

async function guard(operation) {
  try {
    await operation();
  } catch (error) {
    reportError(error);
  }
}

function reportError(error) {
  const message = error instanceof ApiError ? error.message : "Операция не выполнена, но сервис продолжает работу.";
  toast(message, "error");
}

function toast(message, kind = "info") {
  const item = document.createElement("div");
  item.className = `toast ${kind === "error" ? "error" : ""}`;
  item.textContent = message;
  elements.toastRegion.append(item);
  setTimeout(() => item.remove(), 4200);
}

function byId(id) {
  return document.getElementById(id);
}
