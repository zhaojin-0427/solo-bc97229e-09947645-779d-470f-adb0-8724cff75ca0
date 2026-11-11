/* 身份归并入仓页面（原生 JS，无外部依赖）。 */

const PAGE_SIZE = 20;

const $ = (id) => document.getElementById(id);

let currentPage = 1;
let totalPages = 1;
let busy = false;

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[c]);
}

function clearMessage() {
  $("identity-status").textContent = "";
  $("identity-status").className = "status";
  $("identity-error").hidden = true;
  $("identity-error").textContent = "";
}

function showStatus(text, kind = "info") {
  $("identity-error").hidden = true;
  const status = $("identity-status");
  status.className = `status status-${kind}`;
  status.textContent = text;
}

function showError(text) {
  const status = $("identity-status");
  status.className = "status status-error";
  status.textContent = "失败";
  const box = $("identity-error");
  box.textContent = text;
  box.hidden = false;
}

function setBusy(state, button) {
  busy = state;
  if (!button) return;
  if (state) {
    button.dataset.label = button.textContent;
    button.textContent = "处理中…";
    button.disabled = true;
  } else {
    button.textContent = button.dataset.label || button.textContent;
    button.disabled = false;
  }
}

function renderStats(stats) {
  const items = [
    ["直接合号映射", stats.merge_edges, "条"],
    ["正式名单", stats.segment_rows, "行"],
    ["最终客户号", stats.final_customers, "个"],
    ["最长传递链", stats.max_chain_length, "个节点"],
  ];
  $("stat-grid").innerHTML = items.map(([label, value, unit]) => `
    <div class="stat-card">
      <div class="stat-value">${esc(value ?? 0)}<span class="stat-unit">${esc(unit)}</span></div>
      <div class="stat-label">${esc(label)}</div>
    </div>`).join("");
}

function renderMerges(merges) {
  $("merge-summary").textContent = merges.length
    ? `${merges.length} 条直接映射；最终客户号由链路自动传递`
    : "暂无合号映射";
  const body = $("merge-table-body");
  if (!merges.length) {
    body.innerHTML = '<tr><td colspan="4" class="empty-cell">暂无合号映射。上传 CRM 合号 CSV 后显示。</td></tr>';
    return;
  }
  body.innerHTML = merges.map((m) => `
    <tr>
      <td class="mono">${esc(m.old_id)}</td>
      <td class="mono">${esc(m.new_id)}</td>
      <td class="mono strong">${esc(m.final_id)}</td>
      <td class="chain mono">${esc(m.chain.join(" → "))}</td>
    </tr>`).join("");
}

function renderSegments(segments) {
  const total = segments.total || 0;
  totalPages = Math.max(1, Math.ceil(total / (segments.page_size || PAGE_SIZE)));
  currentPage = segments.page || 1;

  $("segment-summary").textContent = `共 ${total} 行`;
  $("page-info").textContent = `第 ${currentPage} / ${totalPages} 页`;
  $("prev-page").disabled = currentPage <= 1;
  $("next-page").disabled = currentPage >= totalPages;

  const body = $("segment-table-body");
  if (!segments.rows.length) {
    body.innerHTML = '<tr><td colspan="3" class="empty-cell">正式名单为空。请先导入分群名单，或上传合号映射。</td></tr>';
    return;
  }
  body.innerHTML = segments.rows.map((row) => `
    <tr>
      <td class="mono strong">${esc(row.cust_id)}</td>
      <td>${esc(row.segment)}</td>
      <td class="mono">${esc(row.biz_date)}</td>
    </tr>`).join("");
}

async function loadState(page = 1) {
  const target = Math.max(1, page);
  try {
    const resp = await fetch(`/api/identity/state?page=${target}&page_size=${PAGE_SIZE}`, {
      cache: "no-store",
    });
    const data = await resp.json();
    if (!data.ok) {
      showError(data.error || "读取状态失败");
      return;
    }
    renderStats(data.stats);
    renderMerges(data.merges);
    renderSegments(data.segments);
  } catch (error) {
    showError("读取状态失败：" + error.message);
  }
}

function mergeMessage(data) {
  const parts = [
    `合号批次成功：新增 ${data.new_edges} 条直接映射`,
    `同向重传 ${data.unchanged_edges} 条`,
    `迁移历史名单 ${data.migrated_segments} 行`,
    `按最终号/分群/业务日去重 ${data.removed_duplicates} 行`,
    `当前正式名单 ${data.segment_rows} 行`,
  ];
  return parts.join("；") + "。";
}

function segmentMessage(data) {
  const parts = [
    `分群名单批次成功：提交 ${data.submitted_rows} 行`,
    `新增 ${data.inserted_rows} 行`,
    `去重忽略 ${data.ignored_rows} 行`,
    `按合号链归到最终号 ${data.resolved_to_final} 行`,
    `当前正式名单 ${data.segment_rows} 行`,
  ];
  return parts.join("；") + "。";
}

async function uploadCsv(kind) {
  if (busy) return;
  const config = kind === "merge"
    ? {
        fileEl: $("merge-file"),
        textEl: $("merge-text"),
        button: $("merge-upload"),
        url: "/api/identity/merges/upload",
        message: mergeMessage,
      }
    : {
        fileEl: $("segment-file"),
        textEl: $("segment-text"),
        button: $("segment-upload"),
        url: "/api/identity/segments/upload",
        message: segmentMessage,
      };

  clearMessage();
  const file = config.fileEl.files[0];
  const form = new FormData();
  if (file) {
    form.append("file", file);
  } else {
    const text = config.textEl.value.trim();
    if (!text) {
      showError("请选择 CSV 文件，或填写 CSV 文本。");
      return;
    }
    form.append("text", text);
  }

  setBusy(true, config.button);
  try {
    const resp = await fetch(config.url, { method: "POST", body: form, cache: "no-store" });
    let data;
    try {
      data = await resp.json();
    } catch (error) {
      throw new Error(`HTTP ${resp.status}`);
    }
    if (!data.ok) {
      showError("整批已拒绝，未写入任何数据：\n" + (data.error || "未知错误"));
      return;
    }
    showStatus(config.message(data), "success");
    if (file) config.fileEl.value = "";
    await loadState(1);
  } catch (error) {
    showError("请求失败：" + error.message);
  } finally {
    setBusy(false, config.button);
  }
}

$("merge-upload").addEventListener("click", () => uploadCsv("merge"));
$("segment-upload").addEventListener("click", () => uploadCsv("segment"));

$("merge-reset").addEventListener("click", () => {
  $("merge-text").value = window.IDENTITY_SAMPLE_MERGE;
  $("merge-file").value = "";
  clearMessage();
});
$("segment-reset").addEventListener("click", () => {
  $("segment-text").value = window.IDENTITY_SAMPLE_SEGMENTS;
  $("segment-file").value = "";
  clearMessage();
});

$("prev-page").addEventListener("click", () => loadState(currentPage - 1));
$("next-page").addEventListener("click", () => loadState(currentPage + 1));

window.addEventListener("DOMContentLoaded", () => loadState(1));
