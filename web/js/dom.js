/**
 * 安全な DOM 操作および数値フォーマッタ。
 * XSS 防止のため、すべてのテキスト描画には textContent / createTextNode のみを使用する。
 */

export function clearNode(node) {
  while (node.firstChild) {
    node.removeChild(node.firstChild);
  }
}

export function el(tag, props = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value === null || value === undefined || value === false) {
      continue;
    }
    if (key === "className") {
      node.className = String(value);
    } else if (key === "text") {
      node.textContent = String(value);
    } else if (key === "dataset" && typeof value === "object") {
      for (const [dk, dv] of Object.entries(value)) {
        if (dv !== undefined && dv !== null) {
          node.dataset[dk] = String(dv);
        }
      }
    } else if (key.startsWith("on") && typeof value === "function") {
      node.addEventListener(key.slice(2).toLowerCase(), value);
    } else if (key === "checked" || key === "disabled" || key === "selected") {
      node[key] = Boolean(value);
    } else if (key === "value") {
      node.value = String(value);
    } else if (key === "title" || key === "href" || key === "type" || key === "id" || key === "role" || key === "colSpan" || key === "placeholder" || key === "ariaLabel") {
      if (key === "ariaLabel") {
        node.setAttribute("aria-label", String(value));
      } else if (key === "colSpan") {
        node.colSpan = Number(value);
      } else {
        node.setAttribute(key, String(value));
      }
    } else {
      node.setAttribute(key, String(value));
    }
  }

  const list = Array.isArray(children) ? children : [children];
  for (const child of list) {
    if (child === null || child === undefined || child === false) {
      continue;
    }
    if (typeof child === "string" || typeof child === "number") {
      node.appendChild(document.createTextNode(String(child)));
    } else if (child instanceof Node) {
      node.appendChild(child);
    }
  }
  return node;
}

export function fmtUsdCompact(val) {
  if (val === null || val === undefined || Number.isNaN(Number(val))) {
    return "—";
  }
  const num = Number(val);
  const abs = Math.abs(num);
  const sign = num < 0 ? "-" : "";
  if (abs >= 1e12) return `${sign}$${(abs / 1e12).toFixed(2)}T`;
  if (abs >= 1e9) return `${sign}$${(abs / 1e9).toFixed(2)}B`;
  if (abs >= 1e6) return `${sign}$${(abs / 1e6).toFixed(1)}M`;
  if (abs >= 1e3) return `${sign}$${(abs / 1e3).toFixed(1)}K`;
  return `${sign}$${abs.toFixed(0)}`;
}

export function fmtPrice(val) {
  if (val === null || val === undefined || Number.isNaN(Number(val))) {
    return "—";
  }
  return `$${Number(val).toFixed(2)}`;
}

export function fmtPct(val, digits = 1, signed = false) {
  if (val === null || val === undefined || Number.isNaN(Number(val))) {
    return "—";
  }
  const pct = Number(val) * 100;
  const prefix = signed && pct > 0 ? "+" : "";
  return `${prefix}${pct.toFixed(digits)}%`;
}

export function fmtPctPoints(val, digits = 2) {
  if (val === null || val === undefined || Number.isNaN(Number(val))) {
    return "—";
  }
  const pct = Number(val) * 100;
  const prefix = pct > 0 ? "+" : "";
  return `${prefix}${pct.toFixed(digits)}%`;
}

export function fmtMultiple(val, suffix = "x") {
  if (val === null || val === undefined || Number.isNaN(Number(val))) {
    return "—";
  }
  return `${Number(val).toFixed(1)}${suffix}`;
}

export function fmtPerShare(val) {
  if (val === null || val === undefined || Number.isNaN(Number(val))) {
    return "—";
  }
  return `$${Number(val).toFixed(2)}`;
}

export function fmtInt(val) {
  if (val === null || val === undefined || Number.isNaN(Number(val))) {
    return "—";
  }
  return Math.round(Number(val)).toLocaleString("en-US");
}

export function fmtStreak(years, isOpen) {
  if (years === null || years === undefined) {
    return "—";
  }
  return `${years}${isOpen ? "+" : ""} yrs`;
}
