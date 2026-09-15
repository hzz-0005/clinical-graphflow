export type Role='admin'|'analyst'|'viewer';
export interface ClinicalTrialCatalogItem{trial_id:string;title:string;phase?:string;status?:string;primary_endpoint?:string}
export type InvestigationSpace='clinical_trial'|'study_registry'|'drug_label'|'safety_signal'|'synthetic_ehr';
export interface ClinicalDataSpace{space:InvestigationSpace;title:string;source:string;data_reality:string;what_it_is:string;examples:string[];can_answer:string;cannot_prove:string;inventory?:Record<string,unknown> & {examples?:Array<{label:string;frequency?:number}>}}
export interface Evidence{evidence_id:string;claim:string;source:string;sql:string;sample_size?:number;evidence_type:string;rows?:Array<Record<string,unknown>>;observation_signal?:string;supports?:string[];contradicts?:string[];quality_flags?:string[]}
export interface InvestigationReport{format_version:string;synthesis_mode:'external_verified'|'deterministic_fallback'|'inconclusive';direct_answer:string;key_findings:string[];evidence_summary:string[];limitations:string[];follow_up:string[];evidence_ids:string[]}
export interface ClinicalCheck{name:string;passed:boolean;detail?:string;message?:string;value?:unknown;threshold?:string;evidence_id?:string}
export interface ClinicalVerification{status?:string;passed?:boolean;effect_estimate?:number;standard_error?:number;ci_lower?:number;ci_upper?:number;confidence_interval?:{lower:number;upper:number;level?:number};checks?:ClinicalCheck[];warnings?:string[];flags?:string[]}
export interface Investigation{investigation_id:string;owner_user_id:string;publication_status:string;research_use_only?:string;state:{question:string;status:string;domain?:string;metric?:string;answer?:string;report?:InvestigationReport;confidence:number;steps:Array<{sequence:number;tool:string;summary?:string}>;evidence:Evidence[];warnings?:string[];audit_metadata?:Record<string,unknown>;verification?:ClinicalVerification;research_notice?:string}}
export interface CdiscRecord{participant_key:string;trial_id:string;site_id:string;arm:string;region?:string}
export interface CdiscQuality{valid:boolean;rows:Record<string,number>;completeness_percent:number;trial_ids:string[];site_ids:string[];arm_counts:Record<string,number>;warnings:string[];errors:string[]}
export interface CdiscPreview{token:string;expires_at:string;quality:CdiscQuality;preview:CdiscRecord[]}
export interface CdiscBatch{batch_id:string;status:string;record_count:number;trial_ids?:string[];published_at?:string;published_by?:string;domain_coverage?:Record<string,boolean>;arm_counts?:Record<string,number>}
export type CdiscPublishedBatch=CdiscBatch & {status:'published';trial_ids:string[];domain_coverage:Record<string,boolean>}
export interface CdiscPublishAssessment{batch_id:string;eligible:boolean;passed_rules:string[];failed_rules:string[];domain_coverage:Record<string,boolean>;arm_counts:Record<string,number>;record_count:number}
export interface FieldSuggestion{target_field:string;source_field?:string;confidence:number;reason:string;required:boolean}
export interface FileMappingSuggestion{filename:string;format:string;domain:'dm'|'adsl'|'adeff';row_count:number;confidence:number;fields:FieldSuggestion[]}
export interface SmartImportInspection{files:FileMappingSuggestion[];ready_for_preview:boolean;warnings:string[];mapping_provider?:string}
export interface DynamicState{investigation_id:string;question:string;status:string;confidence:number;answer?:string;report?:InvestigationReport;steps:Array<{sequence:number;tool:string;inputs?:Record<string,unknown>;status?:string;summary?:string}>;evidence:Evidence[];hypotheses:Array<{hypothesis_id:string;statement:string;status:string;evidence_ids:string[];counter_evidence_ids:string[];rationale?:string|null}>;warnings:string[];audit_metadata:Record<string,unknown>}
export interface ClinicalCatalogField{name:string;inferred_type:string;role:string;nullable?:boolean;null_rate?:number;observed_rows?:number;aliases?:string[]}
export interface ClinicalCatalogDataset{dataset:string;domain:string;domain_version?:string;domain_status:string;is_registered:boolean;source:string;grain?:string;record_count?:number;fields:ClinicalCatalogField[];measures:string[];dimensions:string[];identifiers:string[]}
export interface ClinicalCatalogSnapshot{catalog_version:string;generated_at:string;trial_id?:string;published_batch_id?:string;published_domains:string[];datasets:ClinicalCatalogDataset[];measures:string[];dimensions:string[];data_gaps:string[]}
export interface QuarantineBatch{batch_id:string;status:string;version:number;file_count:number;row_count:number}
export interface QuarantineSchema{batch:QuarantineBatch;schema:{files:Array<{file_id:string;filename:string;format?:string;row_count:number;columns:string[]}>}}
export interface MappingContract{batch_id:string;provider:string;model:string;visibility_level:string;files:Array<{file_id:string;source_filename:string;target_domain:string;status:string;confidence:number;rationale:string;fields:Array<{source:string;target:string;transform:string}>}>}
export interface UnderstandingResult{batch:QuarantineBatch;contract:MappingContract}
export interface ValidationResult{batch:QuarantineBatch;report:{valid:boolean;accepted_rows:number;rejected_rows:number;errors:Array<{code:string;field?:string;message:string}>}}

