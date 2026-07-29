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
      while (index < lines.length && lines[index].trim() && !isBlockStart(lines[index])) {
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
    return protect(
      `<a href="${escapeHtml(safe)}" target="_blank" rel="noopener noreferrer">${escapeHtml(label)}</a>`,
    );
  });
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
