// 保留 Admin 原有表格、排序及操作，在窄屏为每个值补充可见的字段标签。
document.addEventListener("DOMContentLoaded", () => {
  if (!document.body.classList.contains("app-studio")) return;
  const table = document.querySelector("#result_list");
  if (!table) return;
  const labels = Array.from(table.querySelectorAll("thead th"), (header) =>
    (header.querySelector(".text") || header).textContent.trim()
  );
  table.querySelectorAll("tbody tr").forEach((row) => {
    Array.from(row.cells).forEach((cell, index) => {
      if (!cell.classList.contains("action-checkbox")) cell.dataset.label = labels[index];
    });
  });
  table.classList.add("fitstudio-cards");
});
