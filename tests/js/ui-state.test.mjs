import assert from "node:assert/strict";
import test from "node:test";

import {
  deriveComposerState,
  mergeRejectedSubmissionDraft,
  shouldAutoFollowConversation,
  shouldRefreshAcceptedSubmission,
  shouldPollConversation,
  shouldRestoreRejectedSubmission,
} from "../../src/datalab_chat/static/assets/ui-state.js";

test("composer remains writable and sends to the queue during generation", () => {
  const state = deriveComposerState({
    generationStatus: "running",
    busy: false,
    hasProfiles: true,
  });

  assert.equal(state.pending, true);
  assert.equal(state.inputDisabled, false);
  assert.equal(state.sendDisabled, false);
  assert.equal(state.stopHidden, false);
  assert.match(state.statusText, /очеред/i);
  assert.match(state.sendLabel, /очеред/i);
});

test("composer still blocks submit when there is no model profile", () => {
  const state = deriveComposerState({
    generationStatus: null,
    busy: false,
    hasProfiles: false,
  });

  assert.equal(state.inputDisabled, false);
  assert.equal(state.sendDisabled, true);
  assert.equal(state.stopHidden, true);
});

test("polling follows runnable work but pauses blocked or failed queues", () => {
  assert.equal(
    shouldPollConversation({ generationStatus: "running", queuedStatus: null }),
    true,
  );
  assert.equal(
    shouldPollConversation({ generationStatus: null, queuedStatus: "waiting" }),
    true,
  );
  assert.equal(
    shouldPollConversation({ generationStatus: "cancelled", queuedStatus: "waiting" }),
    true,
  );
  assert.equal(
    shouldPollConversation({ generationStatus: "failed", queuedStatus: "waiting" }),
    false,
  );
  assert.equal(
    shouldPollConversation({ generationStatus: null, queuedStatus: "blocked" }),
    false,
  );
});

test("an accepted message keeps refreshing its idle conversation after a failed GET", () => {
  const recovery = {
    generationStatus: null,
    queuedStatus: null,
    recoveryConversationId: "conversation-a",
  };

  assert.equal(
    shouldPollConversation({ ...recovery, currentConversationId: "conversation-a" }),
    true,
    "the stale idle snapshot must not stop recovery polling",
  );
  assert.equal(
    shouldPollConversation({ ...recovery, currentConversationId: "conversation-a" }),
    true,
    "a second failed GET must remain retryable without another POST",
  );
  assert.equal(
    shouldPollConversation({ ...recovery, currentConversationId: "conversation-b" }),
    false,
    "recovery for A must never refresh or navigate back from B",
  );
});

test("rejected submission restoration preserves the exact draft typed in flight", () => {
  const inFlightDraft = "  следующий вопрос\n\n    код с отступом\n";

  assert.equal(
    mergeRejectedSubmissionDraft("исходный запрос", inFlightDraft),
    `исходный запрос\n\n${inFlightDraft}`,
  );
  assert.equal(
    mergeRejectedSubmissionDraft("исходный запрос", " \n  "),
    "исходный запрос \n  ",
  );
});

test("rejected submission is restored only in its originating conversation", () => {
  assert.equal(
    shouldRestoreRejectedSubmission({
      accepted: false,
      originatingConversationId: "conversation-a",
      currentConversationId: "conversation-a",
    }),
    true,
  );
  assert.equal(
    shouldRestoreRejectedSubmission({
      accepted: false,
      originatingConversationId: "conversation-a",
      currentConversationId: "conversation-b",
    }),
    false,
  );
  assert.equal(
    shouldRestoreRejectedSubmission({
      accepted: true,
      originatingConversationId: "conversation-a",
      currentConversationId: "conversation-a",
    }),
    false,
  );
});

test("accepted submission refresh never navigates away from the current conversation", () => {
  assert.equal(
    shouldRefreshAcceptedSubmission({
      accepted: true,
      originatingConversationId: "conversation-a",
      currentConversationId: "conversation-a",
    }),
    true,
  );
  assert.equal(
    shouldRefreshAcceptedSubmission({
      accepted: true,
      originatingConversationId: "conversation-a",
      currentConversationId: "conversation-b",
    }),
    false,
  );
  assert.equal(
    shouldRefreshAcceptedSubmission({
      accepted: false,
      originatingConversationId: "conversation-a",
      currentConversationId: "conversation-a",
    }),
    false,
  );
});

test("conversation updates auto-follow only when the reader was near the bottom", () => {
  assert.equal(
    shouldAutoFollowConversation({
      wasNearBottom: true,
      previousMessageCount: 2,
      nextMessageCount: 4,
      wasPending: true,
      isPending: true,
    }),
    true,
    "queue handoff appends an answer and next prompt while remaining pending",
  );
  assert.equal(
    shouldAutoFollowConversation({
      wasNearBottom: true,
      previousMessageCount: 2,
      nextMessageCount: 2,
      wasPending: true,
      isPending: false,
    }),
    true,
    "terminal status cards should remain visible at the bottom",
  );
  assert.equal(
    shouldAutoFollowConversation({
      wasNearBottom: false,
      previousMessageCount: 2,
      nextMessageCount: 4,
      wasPending: true,
      isPending: false,
    }),
    false,
    "new content must not pull a reader away from older messages",
  );
  assert.equal(
    shouldAutoFollowConversation({
      wasNearBottom: true,
      previousMessageCount: 2,
      nextMessageCount: 2,
      wasPending: true,
      isPending: true,
    }),
    false,
    "an unchanged poll must not cause repeated scrolling",
  );
});
