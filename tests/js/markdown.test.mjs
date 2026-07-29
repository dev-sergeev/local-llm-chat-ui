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

test("renders screenshot-style pipe tables as semantic HTML", () => {
  const html = renderMarkdown(`| Тип фичи | Как строить | Эффект на Gini |
|------------|-------------|-----------------|
| **Lag-фичи** | \`mean/std/skew\` за 3/7/14/30/90 дней | +5–8% |
| **Rolling/Expanding** | Не просто средние | +3–5% |`);

  assert.match(html, /<div class="table-scroll"/);
  assert.match(html, /<table class="markdown-table">/);
  assert.match(
    html,
    /<thead><tr><th>Тип фичи<\/th><th>Как строить<\/th><th>Эффект на Gini<\/th><\/tr><\/thead>/,
  );
  assert.match(
    html,
    /<tbody><tr><td><strong>Lag-фичи<\/strong><\/td><td><code>mean\/std\/skew<\/code> за 3\/7\/14\/30\/90 дней<\/td><td>\+5–8%<\/td><\/tr>/,
  );
  assert.doesNotMatch(html, /<p>\| Тип фичи/);
});

test("does not reinterpret existing block starts as table headers", () => {
  const cases = [
    {
      source: `# Release | Status
--- | ---`,
      expected: /<h1>Release \| Status<\/h1>/,
    },
    {
      source: `- item | value
--- | ---`,
      expected: /<ul><li>item \| value<\/li><\/ul>/,
    },
    {
      source: `> quoted | value
--- | ---`,
      expected: /<blockquote>quoted \| value<\/blockquote>/,
    },
  ];

  for (const { source, expected } of cases) {
    const html = renderMarkdown(source);
    assert.doesNotMatch(html, /<table/);
    assert.match(html, expected);
  }
});

test("ends a table before the next block-level element", () => {
  const cases = [
    {
      source: `A | B
--- | ---
# Heading | outside`,
      expected: /<h1>Heading \| outside<\/h1>/,
    },
    {
      source: `A | B
--- | ---
- item | outside`,
      expected: /<ul><li>item \| outside<\/li><\/ul>/,
    },
    {
      source: `A | B
--- | ---
> quote | outside`,
      expected: /<blockquote>quote \| outside<\/blockquote>/,
    },
    {
      source: `A | B
--- | ---
---`,
      expected: /<hr>/,
    },
    {
      source: ["A | B", "--- | ---", "```text", "x | y", "```"].join("\n"),
      expected: /<pre><code class="language-text">x \| y<\/code><\/pre>/,
    },
  ];

  for (const { source, expected } of cases) {
    const html = renderMarkdown(source);
    assert.match(html, /<table/);
    assert.match(html, /<tbody><\/tbody>/);
    assert.match(html, expected);
  }
});

test("applies delimiter alignment to headers and cells", () => {
  const html = renderMarkdown(`| Слева | Центр | Справа | Обычная |
| :--- | :---: | ---: | --- |
| a | b | c | d |`);

  assert.match(html, /<th class="table-align-left">Слева<\/th>/);
  assert.match(html, /<th class="table-align-center">Центр<\/th>/);
  assert.match(html, /<th class="table-align-right">Справа<\/th>/);
  assert.match(html, /<th>Обычная<\/th>/);
  assert.match(html, /<td class="table-align-left">a<\/td>/);
  assert.match(html, /<td class="table-align-center">b<\/td>/);
  assert.match(html, /<td class="table-align-right">c<\/td>/);
  assert.match(html, /<td>d<\/td>/);
});

test("does not split escaped pipes or pipes inside inline code", () => {
  const html = renderMarkdown(`| Выражение | Описание |
| --- | --- |
| \`left|right\` | Экранированный \\| знак |`);

  assert.match(
    html,
    /<tbody><tr><td><code>left\|right<\/code><\/td><td>Экранированный \| знак<\/td><\/tr><\/tbody>/,
  );
  assert.equal((html.match(/<td/g) ?? []).length, 2);
});

test("unescapes pipes in safe link labels without enabling label HTML", () => {
  const html = renderMarkdown(`| Link | X |
| --- | --- |
| [a\\|b <img>](https://example.test) | y |`);

  assert.match(
    html,
    /<a href="https:\/\/example\.test\/" target="_blank" rel="noopener noreferrer">a\|b &lt;img&gt;<\/a>/,
  );
  assert.doesNotMatch(html, /a\\\|b/);
  assert.doesNotMatch(html, /<img>/);
});

test("keeps malformed table delimiters as ordinary text", () => {
  const html = renderMarkdown(`| Колонка | Значение |
| -- | not-a-delimiter |
| a | b |`);

  assert.doesNotMatch(html, /<table/);
  assert.match(
    html,
    /<p>\| Колонка \| Значение \|<br>\| -- \| not-a-delimiter \|<br>\| a \| b \|<\/p>/,
  );
});

test("escapes table cell HTML and keeps link protocols safe", () => {
  const html = renderMarkdown(`| Тип | Ссылка |
| --- | --- |
| <img src=x onerror=alert(1)> | [bad](javascript:alert) |
| **safe** | [docs](https://bank.example/a?q=1&x=2) |`);

  assert.doesNotMatch(html, /<img/);
  assert.match(html, /&lt;img src=x onerror=alert\(1\)&gt;/);
  assert.doesNotMatch(html, /href="javascript:/);
  assert.match(html, /<td>bad<\/td>/);
  assert.match(html, /<strong>safe<\/strong>/);
  assert.match(html, /href="https:\/\/bank\.example\/a\?q=1&amp;x=2"/);
  assert.match(html, /rel="noopener noreferrer"/);
});

test("URL and HTML helpers are conservative", () => {
  assert.equal(safeExternalUrl("https://example.com/a"), "https://example.com/a");
  assert.equal(safeExternalUrl("http://127.0.0.1:8000"), "http://127.0.0.1:8000/");
  assert.equal(safeExternalUrl("data:text/html,boom"), null);
  assert.equal(escapeHtml(`<&\"'>`), "&lt;&amp;&quot;&#39;&gt;");
});
