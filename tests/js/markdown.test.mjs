import assert from "node:assert/strict";
import test from "node:test";

import {
  escapeHtml,
  renderMarkdown,
  safeExternalUrl,
} from "../../src/datalab_chat/static/assets/markdown.js";

test("renders useful markdown and code controls", () => {
  const html = renderMarkdown(`# Заголовок

Это **важно** и \`точно\`.

\`\`\`python
print("risk")
\`\`\``);

  assert.match(html, /<h1>Заголовок<\/h1>/);
  assert.match(html, /<strong>важно<\/strong>/);
  assert.match(html, /<code>точно<\/code>/);
  assert.match(html, /data-copy-code/);
  assert.match(html, /data-action="copy-code"/);
  assert.match(html, /language-python/);
  assert.match(html, /print\(&quot;risk&quot;\)/);
});

test("escapes raw html and rejects executable links", () => {
  const html = renderMarkdown(`<img src=x onerror=alert(1)>

[bad](javascript:alert(1)) [good](https://bank.example/docs?q=1&x=2)`);

  assert.doesNotMatch(html, /<img/);
  assert.match(html, /&lt;img/);
  assert.doesNotMatch(html, /href="javascript:/);
  assert.match(html, /href="https:\/\/bank\.example\/docs\?q=1&amp;x=2"/);
  assert.match(html, /rel="noopener noreferrer"/);
});

test("handles lists, quotes and paragraphs without losing line breaks", () => {
  const html = renderMarkdown(`- один
- два

> замечание

строка 1
строка 2`);

  assert.match(html, /<ul><li>один<\/li><li>два<\/li><\/ul>/);
  assert.match(html, /<blockquote>замечание<\/blockquote>/);
  assert.match(html, /<p>строка 1<br>строка 2<\/p>/);
});

test("URL and HTML helpers are conservative", () => {
  assert.equal(safeExternalUrl("https://example.com/a"), "https://example.com/a");
  assert.equal(safeExternalUrl("http://127.0.0.1:8000"), "http://127.0.0.1:8000/");
  assert.equal(safeExternalUrl("data:text/html,boom"), null);
  assert.equal(escapeHtml(`<&\"'>`), "&lt;&amp;&quot;&#39;&gt;");
});
