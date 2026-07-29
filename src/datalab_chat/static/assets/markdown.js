const TOKEN_START = "\uE000";
const TOKEN_END = "\uE001";

export function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

export function safeExternalUrl(value) {
  try {
    const parsed = new URL(String(value).trim());
    if (!["http:", "https:"].includes(parsed.protocol)) return null;
    if (parsed.username || parsed.password) return null;
    return parsed.toString();
  } catch {
    return null;
  }
}

export function renderMarkdown(source) {
  try {
    const blockTokens = [];
    const normalized = String(source ?? "").replaceAll("\r\n", "\n").replaceAll("\r", "\n");
    const withCodeBlocks = normalized.replace(
      /```([^\n`]*)\n([\s\S]*?)```/g,
      (_match, rawLanguage, rawCode) => {
        const language = String(rawLanguage).trim().replace(/[^a-zA-Z0-9_+-]/g, "").slice(0, 40);
        const label = language || "code";
        const html = `<div class="code-block"><div class="code-toolbar"><span>${escapeHtml(label)}</span><button type="button" data-action="copy-code" data-copy-code aria-label="Копировать код">Копировать</button></div><pre><code class="language-${escapeHtml(language || "plain")}">${escapeHtml(String(rawCode).replace(/\n$/, ""))}</code></pre></div>`;
        const token = `${TOKEN_START}BLOCK${blockTokens.length}${TOKEN_END}`;
        blockTokens.push(html);
        return `\n${token}\n`;
      },
    );

    const lines = withCodeBlocks.split("\n");
    const output = [];
    let index = 0;

    while (index < lines.length) {
      const line = lines[index];
      if (!line.trim()) {
        index += 1;
        continue;
      }

      const blockIndex = blockTokenIndex(line.trim());
      if (blockIndex !== null) {
        output.push(blockTokens[blockIndex] ?? "");
        index += 1;
        continue;
      }

      const table = parseTable(lines, index);
      if (table !== null) {
        output.push(table.html);
        index = table.nextIndex;
        continue;
      }

      const heading = /^(#{1,6})\s+(.+)$/.exec(line);
      if (heading) {
        const level = heading[1].length;
        output.push(`<h${level}>${renderInline(heading[2])}</h${level}>`);
        index += 1;
        continue;
      }

      if (/^\s*([-*_])(?:\s*\1){2,}\s*$/.test(line)) {
        output.push("<hr>");
        index += 1;
        continue;
      }

      if (/^\s*[-*+]\s+/.test(line)) {
        const items = [];
        while (index < lines.length && /^\s*[-*+]\s+/.test(lines[index])) {
          items.push(lines[index].replace(/^\s*[-*+]\s+/, ""));
          index += 1;
        }
        output.push(`<ul>${items.map((item) => `<li>${renderInline(item)}</li>`).join("")}</ul>`);
        continue;
      }

      if (/^\s*\d+[.)]\s+/.test(line)) {
        const items = [];
        while (index < lines.length && /^\s*\d+[.)]\s+/.test(lines[index])) {
          items.push(lines[index].replace(/^\s*\d+[.)]\s+/, ""));
          index += 1;
        }
        output.push(`<ol>${items.map((item) => `<li>${renderInline(item)}</li>`).join("")}</ol>`);
        continue;
      }

      if (/^\s*>\s?/.test(line)) {
        const quoted = [];
        while (index < lines.length && /^\s*>\s?/.test(lines[index])) {
          quoted.push(lines[index].replace(/^\s*>\s?/, ""));
          index += 1;
        }
        output.push(`<blockquote>${quoted.map(renderInline).join("<br>")}</blockquote>`);
        continue;
      }

      const paragraph = [];
      while (
        index < lines.length &&
        lines[index].trim() &&
        !isBlockStart(lines[index]) &&
        parseTable(lines, index) === null
      ) {
        paragraph.push(lines[index]);
        index += 1;
      }
      if (!paragraph.length) {
        paragraph.push(line);
        index += 1;
      }
      output.push(`<p>${paragraph.map(renderInline).join("<br>")}</p>`);
    }

    return output.join("\n");
  } catch {
    return `<p>${escapeHtml(source ?? "")}</p>`;
  }
}

function renderInline(source) {
  const tokens = [];
  const protect = (html) => {
    const token = `${TOKEN_START}INLINE${tokens.length}${TOKEN_END}`;
    tokens.push(html);
    return token;
  };

  let text = String(source);
  text = text.replace(/`([^`\n]+)`/g, (_match, code) => protect(`<code>${escapeHtml(code)}</code>`));
  text = text.replace(/\[([^\]\n]+)\]\(([^)\s]+)\)/g, (_match, label, rawUrl) => {
    const safe = safeExternalUrl(rawUrl);
    if (!safe) return label;
    const safeLabel = escapeHtml(label).replace(/\\\|/g, "|");
    return protect(
      `<a href="${escapeHtml(safe)}" target="_blank" rel="noopener noreferrer">${safeLabel}</a>`,
    );
  });
  text = text.replace(/\\\|/g, () => protect("|"));
  text = escapeHtml(text);
  text = text.replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");
  text = text.replace(/__([^_\n]+)__/g, "<strong>$1</strong>");
  text = text.replace(/~~([^~\n]+)~~/g, "<del>$1</del>");
  text = text.replace(/(^|[^*])\*([^*\n]+)\*(?!\*)/g, "$1<em>$2</em>");

  return text.replace(new RegExp(`${TOKEN_START}INLINE(\\d+)${TOKEN_END}`, "g"), (_match, rawIndex) => {
    return tokens[Number(rawIndex)] ?? "";
  });
}

function blockTokenIndex(line) {
  const match = new RegExp(`^${TOKEN_START}BLOCK(\\d+)${TOKEN_END}$`).exec(line);
  return match ? Number(match[1]) : null;
}

function parseTable(lines, startIndex) {
  if (startIndex + 1 >= lines.length) return null;
  if (isBlockStart(lines[startIndex])) return null;

  const header = splitTableRow(lines[startIndex]);
  const delimiter = splitTableRow(lines[startIndex + 1]);
  if (!header.hasSeparator || !delimiter.hasSeparator || !header.cells.length) return null;
  if (header.cells.length !== delimiter.cells.length) return null;

  const alignments = delimiter.cells.map(parseTableAlignment);
  if (alignments.some((alignment) => alignment === false)) return null;

  const rows = [];
  let nextIndex = startIndex + 2;
  while (
    nextIndex < lines.length &&
    lines[nextIndex].trim() &&
    !isBlockStart(lines[nextIndex])
  ) {
    const row = splitTableRow(lines[nextIndex]);
    if (!row.hasSeparator) break;
    rows.push(normalizeTableRow(row.cells, header.cells.length));
    nextIndex += 1;
  }

  const headerHtml = header.cells
    .map((cell, cellIndex) => renderTableCell("th", cell, alignments[cellIndex]))
    .join("");
  const bodyHtml = rows
    .map((row) => {
      const cells = row
        .map((cell, cellIndex) => renderTableCell("td", cell, alignments[cellIndex]))
        .join("");
      return `<tr>${cells}</tr>`;
    })
    .join("");

  return {
    html: `<div class="table-scroll" role="region" aria-label="Таблица" tabindex="0"><table class="markdown-table"><thead><tr>${headerHtml}</tr></thead><tbody>${bodyHtml}</tbody></table></div>`,
    nextIndex,
  };
}

function splitTableRow(source) {
  const line = String(source).trim();
  const cells = [];
  let cell = "";
  let codeFenceLength = 0;
  let hasSeparator = false;
  let startsWithSeparator = false;
  let endsWithSeparator = false;

  for (let index = 0; index < line.length; index += 1) {
    const character = line[index];

    if (character === "\\" && index + 1 < line.length) {
      cell += character + line[index + 1];
      index += 1;
      endsWithSeparator = false;
      continue;
    }

    if (character === "`") {
      let fenceLength = 1;
      while (line[index + fenceLength] === "`") fenceLength += 1;
      const fence = "`".repeat(fenceLength);
      cell += fence;
      if (codeFenceLength === 0) codeFenceLength = fenceLength;
      else if (codeFenceLength === fenceLength) codeFenceLength = 0;
      index += fenceLength - 1;
      endsWithSeparator = false;
      continue;
    }

    if (character === "|" && codeFenceLength === 0) {
      if (!hasSeparator) startsWithSeparator = index === 0;
      hasSeparator = true;
      cells.push(cell.trim());
      cell = "";
      endsWithSeparator = index === line.length - 1;
      continue;
    }

    cell += character;
    endsWithSeparator = false;
  }

  cells.push(cell.trim());
  if (startsWithSeparator) cells.shift();
  if (endsWithSeparator) cells.pop();
  return { cells, hasSeparator };
}

function parseTableAlignment(source) {
  const delimiter = String(source).trim();
  if (!/^:?-{3,}:?$/.test(delimiter)) return false;
  if (delimiter.startsWith(":") && delimiter.endsWith(":")) return "center";
  if (delimiter.endsWith(":")) return "right";
  if (delimiter.startsWith(":")) return "left";
  return null;
}

function normalizeTableRow(cells, expectedLength) {
  const normalized = cells.slice(0, expectedLength);
  while (normalized.length < expectedLength) normalized.push("");
  return normalized;
}

function renderTableCell(tag, content, alignment) {
  const classAttribute = alignment ? ` class="table-align-${alignment}"` : "";
  return `<${tag}${classAttribute}>${renderInline(content)}</${tag}>`;
}

function isBlockStart(line) {
  const trimmed = line.trim();
  return (
    blockTokenIndex(trimmed) !== null ||
    /^(#{1,6})\s+/.test(line) ||
    /^\s*[-*+]\s+/.test(line) ||
    /^\s*\d+[.)]\s+/.test(line) ||
    /^\s*>\s?/.test(line) ||
    /^\s*([-*_])(?:\s*\1){2,}\s*$/.test(line)
  );
}
