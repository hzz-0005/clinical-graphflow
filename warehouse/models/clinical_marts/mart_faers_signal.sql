with records as (select * from {{ ref('stg_public_domain_records') }}),
reports as (
 select payload_json->>'SAFETY_REPORT_ID' report_id,payload_json->>'RECEIVED_DATE' received_date,
        payload_json->>'SERIOUS' serious,payload_json->>'OCCUR_COUNTRY' occur_country
 from records where domain_name='FAERS_REPORT'
), drugs as (
 select payload_json->>'SAFETY_REPORT_ID' report_id,payload_json->>'MEDICINAL_PRODUCT' medicinal_product,
        coalesce(payload_json->>'MEDICINAL_PRODUCT_CANONICAL',case trim(trailing '.' from upper(payload_json->>'MEDICINAL_PRODUCT')) when 'METFORMIN' then 'METFORMIN' when 'METFORMIN HYDROCHLORIDE' then 'METFORMIN' when 'SILDENAFIL' then 'SILDENAFIL' when 'SILDENAFIL CITRATE' then 'SILDENAFIL' when 'ASPIRIN' then 'ASPIRIN' end) medicinal_product_canonical,
        coalesce(payload_json->>'MEDICINAL_PRODUCT_ZH',case trim(trailing '.' from upper(payload_json->>'MEDICINAL_PRODUCT')) when 'METFORMIN' then '二甲双胍' when 'METFORMIN HYDROCHLORIDE' then '二甲双胍' when 'SILDENAFIL' then '西地那非' when 'SILDENAFIL CITRATE' then '西地那非' when 'ASPIRIN' then '阿司匹林' end) medicinal_product_zh,
        payload_json->>'DRUG_ROLE' drug_role
 from records where domain_name='FAERS_DRUG'
), reactions as (
 select payload_json->>'SAFETY_REPORT_ID' report_id,payload_json->>'REACTION_TERM' reaction_term,
        coalesce(payload_json->>'REACTION_TERM_ZH',case upper(payload_json->>'REACTION_TERM') when 'ABORTION' then '流产' when 'DEATH' then '死亡' when 'DIARRHOEA' then '腹泻' when 'DRY EYE' then '眼干' when 'DYSPNOEA' then '呼吸困难' when 'FALL' then '跌倒' when 'FATIGUE' then '疲劳' when 'HEADACHE' then '头痛' when 'MATERNAL EXPOSURE DURING PREGNANCY' then '妊娠期母体暴露' when 'OFF LABEL USE' then '超说明书使用' end) reaction_term_zh
 from records where domain_name='FAERS_REACTION'
)
select upper(d.medicinal_product) medicinal_product,d.medicinal_product_canonical,d.medicinal_product_zh,
       r.reaction_term,r.reaction_term_zh,count(distinct p.report_id) report_count,
       count(distinct p.report_id) filter(where p.serious='1') serious_report_count,
       min(p.received_date) first_received_date,max(p.received_date) last_received_date
from reports p join drugs d using(report_id) join reactions r using(report_id)
where d.medicinal_product is not null and r.reaction_term is not null
group by 1,2,3,4,5

