const PENDING_STATUSES = new Set(["queued", "running", "retrying"]);

export function deriveComposerState({ generationStatus, busy, hasProfiles }) {
  const pending = PENDING_STATUSES.has(generationStatus);
  let statusText = "Enter — отправить · Shift+Enter — новая строка";
  if (pending) {
    statusText = generationStatus === "retrying"
      ? "Выполняется повтор · новое сообщение попадёт в очередь"
      : "Модель отвечает · новое сообщение попадёт в очередь";
  } else if (!hasProfiles) {
    statusText = "Добавьте профиль модели, чтобы отправить сообщение";
  }

  return {
    pending,
    stopHidden: !pending,
    sendDisabled: Boolean(busy || !hasProfiles),
    inputDisabled: false,
    statusText,
    sendLabel: pending ? "Добавить сообщение в очередь" : "Отправить сообщение",
  };
}

export function shouldPollConversation({
  generationStatus,
  queuedStatus,
  recoveryConversationId = null,
  currentConversationId = null,
}) {
  if (
    recoveryConversationId
    && recoveryConversationId === currentConversationId
  ) return true;
  if (PENDING_STATUSES.has(generationStatus)) return true;
  return queuedStatus === "waiting"
    && (generationStatus == null || generationStatus === "cancelled");
}

export function mergeRejectedSubmissionDraft(submittedContent, currentDraft) {
  const submitted = String(submittedContent);
  const draft = String(currentDraft);
  const separator = draft.trim() ? "\n\n" : "";
  return `${submitted}${separator}${draft}`;
}

export function shouldRestoreRejectedSubmission({
  accepted,
  originatingConversationId,
  currentConversationId,
}) {
  return !accepted && originatingConversationId === currentConversationId;
}

export function shouldRefreshAcceptedSubmission({
  accepted,
  originatingConversationId,
  currentConversationId,
}) {
  return Boolean(
    accepted
    && originatingConversationId
    && originatingConversationId === currentConversationId,
  );
}

export function shouldAutoFollowConversation({
  wasNearBottom,
  previousMessageCount,
  nextMessageCount,
  wasPending,
  isPending,
}) {
  if (!wasNearBottom) return false;
  const messagesGrew = nextMessageCount > previousMessageCount;
  const becameTerminal = Boolean(wasPending && !isPending);
  return messagesGrew || becameTerminal;
}

export function readStorageValue(storageFactory, key) {
  try {
    const storage = storageFactory();
    return storage ? storage.getItem(key) : null;
  } catch {
    return null;
  }
}

export function writeStorageValue(storageFactory, key, value) {
  try {
    const storage = storageFactory();
    if (!storage) return false;
    storage.setItem(key, value);
    return true;
  } catch {
    return false;
  }
}

export function removeStorageValue(storageFactory, key) {
  try {
    const storage = storageFactory();
    if (!storage) return false;
    storage.removeItem(key);
    return true;
  } catch {
    return false;
  }
}
