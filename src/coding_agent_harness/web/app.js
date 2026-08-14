const text = (element, value) => {
  if (element) element.textContent = String(value ?? "-");
};

const render = (report) => {
  if (report.schema_version !== "1.0") throw new Error("Unsupported report version");
  text(document.querySelector("[data-session]"), report.session_id);
  text(document.querySelector("[data-status]"), report.status);
  const timeline = document.querySelector("[data-timeline]");
  for (const action of report.actions ?? []) {
    const item = document.createElement("li");
    text(item, `${action.tool ?? "-"} / ${action.decision ?? "-"} / ${action.reason_code ?? "-"}${action.path ? ` / ${action.path}` : ""}`);
    timeline?.append(item);
  }
  const approvals = document.querySelector("[data-approvals]");
  for (const approval of report.approvals ?? []) {
    const item = document.createElement("li");
    text(item, approval.status);
    approvals?.append(item);
  }
  const validations = document.querySelector("[data-validations]");
  for (const validation of report.validations ?? []) {
    const item = document.createElement("li");
    text(item, `${validation.validator_id ?? "-"} / ${validation.stage ?? "-"} / ${validation.status ?? "-"}`);
    validations?.append(item);
  }
};

fetch("./mock-report.json")
  .then((response) => response.json())
  .then(render)
  .catch((error) => text(document.querySelector("[data-error]"), error.message));
