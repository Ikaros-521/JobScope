import type React from "react";
import { Button } from "../ui";
import { Icon } from "../Icon";
import { BossImport } from "../BossImport";

// 岗位研究的五个弹窗（导入岗位/补充资料/Boss 导入/批量编辑/确认删除）的容器。
// 2026-11-28 自 JobResearch.tsx 拆出：setModal/setBatchDraft 等以同名 props 透传，
// JSX 主体零改动；Boss 导入完成后的三联刷新经 onBossImported 回调回主容器。
export function JobResearchDialogs({
  modal,
  setModal,
  busy,
  batchEdit,
  batchDraft,
  setBatchDraft,
  setTagsChanged,
  confirmDelete,
  importRecord,
  submitBatchEdit,
  submitBatchDelete,
  perform,
  job,
  onBossImported,
}: {
  modal:
    "job" | "evidence" | "boss-import" | "batch-edit" | "confirm-delete" | null;
  setModal: (
    value:
      | "job"
      | "evidence"
      | "boss-import"
      | "batch-edit"
      | "confirm-delete"
      | null,
  ) => void;
  busy: string;
  batchEdit: { ids: string[] } | null;
  batchDraft: { location: string; salary: string; tags: string };
  setBatchDraft: (draft: {
    location: string;
    salary: string;
    tags: string;
  }) => void;
  setTagsChanged: React.Dispatch<React.SetStateAction<boolean>>;
  confirmDelete: { ids: string[]; label: string } | null;
  importRecord: (
    event: React.FormEvent<HTMLFormElement>,
  ) => Promise<void> | void;
  submitBatchEdit: (
    event: React.FormEvent<HTMLFormElement>,
  ) => Promise<void> | void;
  submitBatchDelete: () => Promise<void>;
  perform: (key: string, action: () => Promise<void>) => void;
  job: { company?: string; title?: string } | undefined;
  onBossImported: () => void;
}) {
  return (
    <>
      {modal && (
        <div
          className="modal-backdrop"
          onMouseDown={(event) => {
            if (event.target === event.currentTarget && !busy) setModal(null);
          }}
        >
          <section
            className="modal paper-panel"
            role="dialog"
            aria-modal="true"
            aria-labelledby="modal-title"
            onKeyDown={(event) => {
              if (event.key === "Escape" && !busy) {
                event.stopPropagation();
                setModal(null);
              }
              if (event.key === "Tab") {
                const focusable = Array.from(
                  event.currentTarget.querySelectorAll<HTMLElement>(
                    "button:not(:disabled), input:not(:disabled), textarea:not(:disabled), select:not(:disabled), a[href]",
                  ),
                );
                const first = focusable[0],
                  last = focusable[focusable.length - 1];
                if (event.shiftKey && document.activeElement === first) {
                  event.preventDefault();
                  last?.focus();
                } else if (!event.shiftKey && document.activeElement === last) {
                  event.preventDefault();
                  first?.focus();
                }
              }
            }}
          >
            <div className="section-heading">
              <div>
                <h2 id="modal-title">
                  {modal === "job"
                    ? "导入岗位"
                    : modal === "evidence"
                      ? "补充公司资料"
                      : modal === "boss-import"
                        ? "从 Boss 导入"
                        : modal === "batch-edit"
                          ? "批量编辑档案"
                          : "确认删除岗位"}
                </h2>
              </div>
              <button
                className="icon-button"
                aria-label="关闭弹窗"
                onClick={() => setModal(null)}
                disabled={Boolean(busy)}
              >
                <Icon name="close" />
              </button>
            </div>
            <p className="muted body-small">
              {modal === "job"
                ? "粘贴原始岗位信息，来源将标记为“用户导入”。"
                : modal === "evidence"
                  ? "保留原文和来源，帮助后续核实。请勿填写 Cookie 或访问令牌。"
                  : modal === "boss-import"
                    ? "你在项目浏览器里看到的 Boss 页面就是导入源：捕获当前页、粘贴分享链接，重复岗位自动归并。"
                    : modal === "batch-edit"
                      ? `将应用到已选的 ${batchEdit?.ids.length ?? 0} 份档案；留空的字段保持不变，薪资按原文覆盖（如 15–25K·13薪）。`
                      : `将删除${confirmDelete?.label || "所选岗位"}，连同其岗位证据、适配报告与岗位对话，此操作不可恢复。`}
            </p>
            {(modal === "job" || modal === "evidence") && (
              <>
                <form onSubmit={importRecord}>
                  <label className="field-label" htmlFor="import-title">
                    {modal === "job" ? "岗位名称" : "资料标题"} <span>*</span>
                  </label>
                  <input
                    id="import-title"
                    name="title"
                    required
                    maxLength={300}
                    autoFocus
                  />
                  <label className="field-label" htmlFor="import-company">
                    公司名称 <span>*</span>
                  </label>
                  <input
                    id="import-company"
                    name="company"
                    required
                    maxLength={300}
                    defaultValue={modal === "evidence" ? job?.company : ""}
                  />
                  {modal === "job" ? (
                    <div className="form-two">
                      <div>
                        <label
                          className="field-label"
                          htmlFor="import-location"
                        >
                          工作地点
                        </label>
                        <input
                          id="import-location"
                          name="location"
                          maxLength={200}
                        />
                      </div>
                      <div>
                        <label className="field-label" htmlFor="import-salary">
                          原始薪资描述
                        </label>
                        <input
                          id="import-salary"
                          name="salary"
                          maxLength={300}
                          placeholder="保留原文，如 20–25K·13薪"
                        />
                      </div>
                    </div>
                  ) : (
                    <>
                      <label className="field-label" htmlFor="import-kind">
                        资料类型
                      </label>
                      <select
                        id="import-kind"
                        name="kind"
                        defaultValue="user_material"
                      >
                        <option value="user_material">用户提供材料</option>
                        <option value="employee_statement">员工自述</option>
                        <option value="company_statement">企业自述</option>
                        <option value="disclosure">正式披露</option>
                        <option value="media_report">媒体报道</option>
                      </select>
                    </>
                  )}
                  <label className="field-label" htmlFor="import-url">
                    原始链接（可选）
                  </label>
                  <input
                    id="import-url"
                    name="url"
                    type="url"
                    maxLength={2000}
                    placeholder="https://"
                  />
                  <label className="field-label" htmlFor="import-body">
                    {modal === "job" ? "岗位描述" : "资料原文"} <span>*</span>
                  </label>
                  <textarea
                    id="import-body"
                    name={modal === "job" ? "description" : "text"}
                    required
                    rows={7}
                    maxLength={100000}
                  />
                  <div className="modal-actions">
                    <button
                      type="button"
                      className="button secondary"
                      onClick={() => setModal(null)}
                      disabled={Boolean(busy)}
                    >
                      取消
                    </button>
                    <Button
                      className="button primary"
                      type="submit"
                      busy={busy === "import"}
                      disabled={Boolean(busy)}
                    >
                      保存资料
                      <Icon name="check" size={16} />
                    </Button>
                  </div>
                </form>
              </>
            )}
            {modal === "boss-import" && (
              <BossImport
                onClose={() => setModal(null)}
                onImported={onBossImported}
              />
            )}
            {modal === "batch-edit" && batchEdit && (
              <form onSubmit={submitBatchEdit}>
                <label className="field-label" htmlFor="batch-location">
                  工作地点
                </label>
                <input
                  id="batch-location"
                  value={batchDraft.location}
                  onChange={(event) =>
                    setBatchDraft({
                      ...batchDraft,
                      location: event.target.value,
                    })
                  }
                  maxLength={300}
                  autoFocus
                />
                <label className="field-label" htmlFor="batch-salary">
                  薪资描述
                </label>
                <input
                  id="batch-salary"
                  value={batchDraft.salary}
                  onChange={(event) =>
                    setBatchDraft({ ...batchDraft, salary: event.target.value })
                  }
                  maxLength={300}
                  placeholder="15–25K·13薪"
                />
                <label className="field-label" htmlFor="batch-tags">
                  个人标签（逗号分隔，可清空；平台标签保留）
                </label>
                <input
                  id="batch-tags"
                  value={batchDraft.tags}
                  onChange={(event) => {
                    setTagsChanged(true);
                    setBatchDraft({ ...batchDraft, tags: event.target.value });
                  }}
                  maxLength={500}
                  placeholder="已核实, 优先投递"
                />
                <div className="modal-actions">
                  <button
                    type="button"
                    className="button secondary"
                    onClick={() => setModal(null)}
                    disabled={Boolean(busy)}
                  >
                    取消
                  </button>
                  <Button
                    className="button primary"
                    type="submit"
                    busy={busy === "batch-edit"}
                    disabled={Boolean(busy)}
                  >
                    保存修改
                    <Icon name="check" size={16} />
                  </Button>
                </div>
              </form>
            )}
            {modal === "confirm-delete" && confirmDelete && (
              <div className="modal-actions">
                <button
                  type="button"
                  className="button secondary"
                  onClick={() => setModal(null)}
                  disabled={Boolean(busy)}
                >
                  取消
                </button>
                <Button
                  className="button danger"
                  busy={busy === "batch-delete"}
                  disabled={Boolean(busy)}
                  onClick={() =>
                    void perform("batch-delete", submitBatchDelete)
                  }
                >
                  确认删除
                </Button>
              </div>
            )}
          </section>
        </div>
      )}
    </>
  );
}
