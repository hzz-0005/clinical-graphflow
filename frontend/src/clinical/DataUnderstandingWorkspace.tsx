import { useState } from "react";
import { Button, Tag } from "@carbon/react";
import { advanceQuarantine, profileQuarantine, understandQuarantine, uploadQuarantine, validateQuarantine } from "../api";
import type { MappingContract, QuarantineBatch, QuarantineSchema, Role, ValidationResult } from "../types";

export function DataUnderstandingWorkspace({ role }: { role: Role }) {
  const [files, setFiles] = useState<File[]>([]);
  const [batch, setBatch] = useState<QuarantineBatch | null>(null);
  const [profile, setProfile] = useState<QuarantineSchema | null>(null);
  const [contract, setContract] = useState<MappingContract | null>(null);
  const [validation, setValidation] = useState<ValidationResult["report"] | null>(null);
  const [error, setError] = useState("");
  const upload = async () => {
    try {
      setBatch(await uploadQuarantine(files, role));
      setProfile(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "上传失败");
    }
  };
  const understand = async () => { if (batch) try { const result=await understandQuarantine(batch,role);setBatch(result.batch);setContract(result.contract); } catch(e){setError(e instanceof Error?e.message:"理解失败")} };
  const validate = async () => { if(batch) try {const result=await validateQuarantine(batch,role);setBatch(result.batch);setValidation(result.report)} catch(e){setError(e instanceof Error?e.message:"验证失败")} };
  const advance = async(action:"approve"|"publish") => {if(batch) try{setBatch(await advanceQuarantine(batch,action))}catch(e){setError(e instanceof Error?e.message:"操作失败")}};
  const inspect = async () => {
    if (batch)
      try {
        const result = await profileQuarantine(batch, role);
        // 画像在服务端会把批次推进到 profiled（并递增乐观锁版本）。客户端必须回写批次，
        // 否则横幅停留在 parsed，下一步「识别数据域与字段」永远不会出现，生命周期会卡死。
        setProfile(result);
        setBatch(result.batch);
      } catch (e) {
        setError(e instanceof Error ? e.message : "画像失败");
      }
  };
  return (
    <section className="case-sheet">
      <header>
        <div>
          <h1>临床数据理解</h1>
          <p>
            原始文件先进入隔离区。系统只向模型提供字段结构、统计画像与关系候选。
          </p>
        </div>
        <Tag type="teal">隐私级别 L1</Tag>
      </header>
      <div className="drop-zone">
        <label htmlFor="clinical-files">选择临床数据文件</label>
        <input
          id="clinical-files"
          aria-label="选择临床数据文件"
          type="file"
          multiple
          onChange={(e) => setFiles(Array.from(e.target.files ?? []))}
        />
        <p>
          默认支持 Excel（XLSX）、CSV、TSV、JSON、JSONL；Parquet、XPT 由已安装适配器决定。
        </p>
        <Button disabled={!files.length || role === "viewer"} onClick={upload}>
          上传到隔离区
        </Button>
      </div>
      {batch && (
        <div className="quarantine-banner">
          <strong>已进入隔离区</strong>
          <span>
            {batch.row_count} 行，状态：{batch.status}
          </span>
          {batch.status==="parsed"&&<Button kind="ghost" onClick={inspect}>生成安全数据画像</Button>}
          {batch.status==="profiled"&&<Button kind="ghost" onClick={understand}>识别数据域与字段</Button>}
          {batch.status==="mapping_proposed"&&<Button kind="ghost" onClick={validate}>验证映射合同</Button>}
          {batch.status==="validated"&&role==="admin"&&<Button kind="ghost" onClick={()=>advance("approve")}>管理员批准</Button>}
          {batch.status==="approved"&&role==="admin"&&<Button kind="ghost" onClick={()=>advance("publish")}>发布数据版本</Button>}
        </div>
      )}
      {contract&&<section className="schema-ledger"><h2>映射建议与解释</h2>{contract.files.map(file=><article key={file.file_id}><div><strong>{file.source_filename} → {file.target_domain}</strong><Tag>{Math.round(file.confidence*100)}% 可信</Tag></div><p>{file.rationale}</p><p>{file.fields.map(field=>`${field.source} → ${field.target}`).join("；")||"未强制映射，保留为自定义候选域"}</p></article>)}</section>}
      {validation&&<section className="schema-ledger"><h2>确定性验证结果</h2><p>{validation.valid?`通过：${validation.accepted_rows} 行可发布`:`未通过：${validation.rejected_rows} 行被拒绝`}</p>{validation.errors.map((item,index)=><p key={index}>{item.field??"文件"}：{item.message}</p>)}</section>}
      {profile && (
        <section className="schema-ledger">
          <h2>文件结构与字段</h2>
          {profile.schema.files.map((file) => (
            <article key={file.file_id}>
              <div>
                <strong>{file.filename}</strong>
                <span>{file.row_count} 行</span>
              </div>
              <p>{file.columns.join("、")}</p>
            </article>
          ))}
        </section>
      )}
      {error && (
        <p role="alert" className="clinical-error">
          {error}
        </p>
      )}
    </section>
  );
}

