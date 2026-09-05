# erpnext 关系表（bridge 角色）清单

> 本体 erpnext `c9820a62-66cb-4cc0-a2cc-36ff0bde5c77`｜全量 315 个（`table_role=bridge`，含明细/子表、桥接表、业务事实流水）

| 板块 | 数量 |
|---|---|
| 系统表 | 69 |
| 商业运营 | 57 |
| 生产制造 | 52 |
| 资金与支付 | 44 |
| 物料与库存管理 | 31 |
| 公共主数据 | 22 |
| 业财管理 | 19 |
| 资产管理 | 16 |
| 账款催收 | 2 |
| 订阅定价 | 1 |
| 项目分类 | 2 |

## 系统表（69）

| 显示名 | 标识名 | 状态 | ID |
|---|---|---|---|
| 预付款流水 | `advance_payment_ledger_entry` | suggested | `6ec3feab-88ba-407a-a27e-f20cdd26b8d3` |
| 预付税费明细 | `advance_taxes_and_charges` | suggested | `a270ce63-d741-481d-bf22-9e787c0d0ba8` |
| 自动重复日 | `auto_repeat_day` | suggested | `373a4c8d-fa37-459c-8a5e-f428dd64f445` |
| 自动重复用户 | `auto_repeat_user` | suggested | `63123174-4b3e-423b-a8ea-28d99556dcb1` |
| 可用时段 | `availability_of_slots` | suggested | `9a2f4784-34e0-4bd5-a909-42113ebc102e` |
| 一揽子订单明细 | `blanket_order_item` | suggested | `35b7edf9-9a9e-47f3-bcbd-c3e4380bf12b` |
| 预算账户明细 | `budget_account` | suggested | `346d8849-abc6-486b-8379-1c9e7d4d7651` |
| 预算分配明细 | `budget_distribution` | suggested | `8b7aa300-f71d-4c86-adb5-961de4860b53` |
| 已关闭文档 | `closed_document` | suggested | `1501c801-b07c-4089-b3f5-c429215ef55c` |
| 代码表客户分组宽表 | `code_list_customer_group_wide` | edited | `ba6353d8-99e2-43c6-a583-f4477c3d9db4` |
| 竞争对手明细 | `competitor_detail` | suggested | `3befe0d7-281f-46fc-809c-b7e262afb4fd` |
| CRM备注 | `crm_note` | suggested | `8097d0bd-af3f-467e-adee-32b51041e1fc` |
| 依赖任务 | `dependent_task` | suggested | `2ac04b89-2ee7-4fb3-8a06-c3a23f67b4e5` |
| 折旧明细 | `depreciation_schedule` | suggested | `21248aa1-3fad-48b8-ab2f-2fb8e053b0f8` |
| 折扣发票 | `discounted_invoice` | suggested | `92f62544-d154-4334-95c8-7608e715c764` |
| 驾驶证准驾类别 | `driving_license_category` | suggested | `f6f91130-606f-421b-9a49-e5907004b7cb` |
| 财务报表行 | `financial_report_row` | suggested | `0411d052-3ccb-4f4c-9189-07313de635cf` |
| 角色分配 | `has_role` | suggested | `611aa593-7f6c-4b0c-8e0a-25b8f65ceaa7` |
| 行业类型 | `industry_type` | edited | `f1a89995-a3b7-4304-8965-01a1d2f7281d` |
| 到岸成本明细 | `landed_cost_item` | suggested | `c2d73b50-e2fe-46ec-a973-71dcc8a50c8b` |
| 到岸成本采购入库单明细 | `landed_cost_purchase_receipt` | suggested | `855304aa-6180-44fd-b74b-782e202014e5` |
| 到岸成本税费明细 | `landed_cost_taxes_and_charges` | suggested | `97f94f88-4a23-4e4d-8b12-9b67b80463b6` |
| 到岸成本供应商发票明细 | `landed_cost_vendor_invoice` | suggested | `2c51302c-8eef-40de-a9b1-66ec2e249d76` |
| 关联位置 | `linked_location` | suggested | `e4ef0c2c-14af-4951-be46-05e0475827e4` |
| 流失原因明细 | `lost_reason_detail` | suggested | `bc0720ee-47e0-48a1-b762-efa9eee4d606` |
| 主生产计划 | `master_production_schedule` | edited | `96ab16e8-7404-4550-8bb7-2232ea2adb67` |
| 主生产计划明细 | `master_production_schedule_item` | suggested | `2fcc49b9-c4b3-4e54-8c1b-ef615b06c2e5` |
| 里程碑 | `milestone` | suggested | `be100fe0-ee95-43c3-a4e3-447586201eb3` |
| 月度分摊比例 | `monthly_distribution_percentage` | suggested | `6160b7d8-cb81-4417-a6dd-a7550a5f9fa5` |
| 不符合项报告 | `non_conformance` | suggested | `f7c60b6b-afb3-404c-a1bf-912d1aab2a16` |
| 笔记阅读记录 | `note_seen_by` | suggested | `b39ea1fd-ea51-4dfe-b9a2-b69fa58e9ee7` |
| 期初发票工具明细 | `opening_invoice_tool_item` | suggested | `97c79886-91fe-4c37-aaad-e0f8f908a925` |
| 订单客户分组宽表 | `order_customer_group_wide` | edited | `984782db-9d03-4172-810c-42dc4d8611c1` |
| 逾期付款 | `overdue_payment` | suggested | `861a60d1-23a1-49c1-bcd4-65275599381a` |
| 打包明细 | `packed_item` | suggested | `9faf2b2c-a52c-4f4c-a540-c598b645a4f1` |
| 挂钩货币明细 | `pegged_currency_detail` | suggested | `f04c4d18-6429-4bc4-b1cc-683e8f0cd1c1` |
| 期末结账凭证 | `period_closing_voucher` | suggested | `61119384-c990-4afe-8f6b-a1c52f7d4559` |
| 期末结转凭证明细 | `period_closing_voucher_detail` | suggested | `05ea391f-8484-4f9e-9790-8f35cbed6392` |
| 期末结转凭证 | `process_period_closing_voucher` | suggested | `979574b2-117a-494c-a2f4-1be59f412556` |
| 产品包明细 | `product_bundle_item` | suggested | `e7c6f45c-808d-41ea-b38c-d698ec7373d7` |
| 成本中心 | `psoa_cost_center` | edited | `f605cefe-1ab3-43d6-aacc-9a33a338d2f9` |
| 项目 | `psoa_project` | edited | `d797bfc4-5f42-4a74-8ad9-f03ecc8bd9a5` |
| 采购发票 | `purchase_invoice` | suggested | `b660e7b0-943d-41be-ad08-ab0d24612abd` |
| 采购发票预付 | `purchase_invoice_advance` | suggested | `07fadedd-167c-4fcd-9c9b-a38ee4b7344c` |
| 采购发票明细 | `purchase_invoice_item` | suggested | `bcccbc31-4247-436a-b56f-d9dbf881d540` |
| 采购订单 | `purchase_order` | suggested | `009ab155-1dc4-493d-b31f-77d36ff97655` |
| 采购订单明细 | `purchase_order_item` | suggested | `c8b1e458-8272-424f-95b8-049e6b334aa3` |
| 采购订单供应明细 | `purchase_order_item_supplied` | suggested | `f3fb7b26-dcf7-4d7f-b002-558aaad7e6eb` |
| 采购收货单 | `purchase_receipt` | suggested | `e6a72e83-f08e-4a65-808e-e31883c6e6ba` |
| 采购入库明细 | `purchase_receipt_item` | suggested | `0d0a0903-ec8d-4768-a176-363044ac8294` |
| 采购收货供应明细 | `purchase_receipt_item_supplied` | suggested | `8db695db-ed23-42ed-863b-45b6e7b197c8` |
| 采购税费与附加费 | `purchase_taxes_and_charges` | suggested | `164bc730-ec00-4662-aaea-4192583bf6ef` |
| 再订购设置 | `reorder` | suggested | `995e32f1-5a47-4d54-b271-dd77ed76eb26` |
| 回复收件地址 | `reply_to_address` | suggested | `9cc1a1bb-a5cd-4dfe-a36b-d6a2bace692e` |
| 股权余额 | `share_balance` | suggested | `fd55dce8-ee28-42ea-ba62-2b991abf31be` |
| 股权转让 | `share_transfer` | suggested | `f86758e2-5817-4522-92c4-d1007b309b09` |
| SLA达成状态 | `sla_fulfilled_on_status` | suggested | `24f92128-4f47-407a-b1de-b65d25d62f5b` |
| 对账单成本中心 | `statement_of_accounts_cc` | suggested | `43b755e9-667a-4906-8773-9e33bc6f2f6a` |
| 对账单客户 | `statement_of_accounts_customer` | suggested | `363c38ce-5201-4b17-93f9-89bb12d78754` |
| 子工序明细 | `sub_operation` | suggested | `d914529f-a650-45c5-aef8-76153e6d9abc` |
| 标签关联 | `tag_link` | suggested | `21d3891b-9d6d-4ef2-8cf8-69823ae4ded4` |
| 目标明细 | `target_detail` | suggested | `09d2874a-550a-43a7-8767-c0af19e70c95` |
| 待办任务 | `todo` | suggested | `cbb55386-9723-4082-8525-d9c526d957c1` |
| 交易删除记录明细 | `transaction_deletion_record_details` | suggested | `543375f4-7c32-4bd6-bc25-77abae800415` |
| 交易删除记录项 | `transaction_deletion_record_item` | suggested | `296a562b-b2c1-4f72-8c77-d0ddedb4b098` |
| 交易删除待删清单 | `transaction_deletion_record_to_delete` | suggested | `c1480650-a925-4cdb-9f17-6e5d60d97a2c` |
| 阿联酋增值税账户 | `uae_vat_account` | suggested | `4efe79ce-cd29-42f6-844d-e8ab9f30fd31` |
| 变体属性 | `variant_attribute` | suggested | `5b392716-2d70-4283-a009-b5add8fbb0b7` |
| 变体字段 | `variant_field` | suggested | `0b7684d7-058e-4dac-9bec-3747ebbcc629` |

## 商业运营（57）

| 显示名 | 标识名 | 状态 | ID |
|---|---|---|---|
| 营销邮件计划 | `campaign_email_schedule` | suggested | `b8a6d6bb-7d9c-424b-98d5-ffc6fb8572f5` |
| 营销活动行项目 | `campaign_item` | suggested | `02861aa4-85a2-403c-bf38-e26637c2e82e` |
| 客户信用额度 | `customer_credit_limit` | suggested | `646fb44f-74e5-42ce-b045-0b74c2746000` |
| 客户分组成员 | `customer_group_item` | suggested | `8230c570-6266-441e-9495-9b7e655f24ac` |
| 客户商品明细 | `customer_item` | suggested | `ce48c74e-5e63-4cc3-99e5-5dc5865eb006` |
| 供应商客户编号 | `customer_number_at_supplier` | suggested | `53b2a415-9eea-49df-aca7-2e6e12b2c1c9` |
| 送货单 | `delivery_note` | suggested | `2868b217-fe36-421b-9ed6-276ce0aeca23` |
| 邮件摘要收件人 | `email_digest_recipient` | suggested | `d4cee9b1-8ade-44f0-bcda-69e26e666d56` |
| 邮件组成员 | `email_group_member` | suggested | `1be99592-e34c-42f1-9a15-00e1a6ba63cf` |
| 假日 | `holiday` | suggested | `215819f5-f42b-445b-94cf-24ec3dc81952` |
| 安装单 | `installation_note` | suggested | `b5a3a872-be50-405f-a805-cefea3c519f8` |
| 安装单明细 | `installation_note_item` | suggested | `e06901fc-60fd-4bbb-b576-ab6b4dbab4b5` |
| 积分流水 | `loyalty_point_entry` | suggested | `784154f3-08da-4753-85f4-351ccaa25b16` |
| 积分兑换明细 | `loyalty_point_redemption` | suggested | `d410e4b4-f5a0-4b90-9398-ecef6abe73aa` |
| 积分采集规则 | `loyalty_program_collection` | suggested | `26ff291a-4126-46f5-8923-e50ba36fe02f` |
| 维保计划明细 | `maintenance_schedule_detail` | suggested | `119018fd-7cf5-4be4-af75-1a17779c8318` |
| 维保项目 | `maintenance_schedule_item` | suggested | `10a9ff92-2db0-4b73-87b5-0f1a5c450f49` |
| 维保团队成员 | `maintenance_team_member` | suggested | `ba82729f-c323-45c2-9b7c-72c75ee19f42` |
| 维修巡检 | `maintenance_visit` | suggested | `7ae5380c-4472-49ff-a2a3-0448b9531687` |
| 维保访问目的 | `maintenance_visit_purpose` | suggested | `1e4db2d6-f0a7-46ba-b370-c16af6c01d62` |
| 商机明细 | `opportunity_item` | suggested | `54164069-68cd-4e0d-b42d-f5c38bee6ed3` |
| 商机丢单原因明细 | `opportunity_lost_reason_detail` | suggested | `82b3b6f8-221b-4597-92df-b0fb894f3f5d` |
| POS结账 | `pos_closing_entry` | suggested | `a0fd88f9-0f61-46d6-9b0d-390590864d5d` |
| POS结账明细 | `pos_closing_entry_detail` | suggested | `27ea5e5e-a0fd-4a17-8f88-23b50b90d9ba` |
| POS结账税费明细 | `pos_closing_entry_taxes` | suggested | `333cb3ce-efbd-492b-aaa3-544b8970efef` |
| POS销售发票 | `pos_invoice` | suggested | `4b0576c4-2a29-4767-9252-225eac0bfb65` |
| POS发票明细 | `pos_invoice_item` | suggested | `abdd009d-f6a1-403f-b716-1cc7b037080c` |
| POS发票合并日志 | `pos_invoice_merge_log` | suggested | `d06bfbf9-a549-4f48-9cfa-1c7979bb9218` |
| POS发票引用 | `pos_invoice_reference` | suggested | `9985582a-0ae5-43d0-9fa9-d230478a9e97` |
| POS商品分组 | `pos_item_group` | suggested | `278a7031-435f-455e-a3f1-4a339660034d` |
| POS开班 | `pos_opening_entry` | suggested | `bd1dfaa3-3d2d-4de4-8814-4b565410f9c9` |
| POS开班明细 | `pos_opening_entry_detail` | suggested | `81a4ec30-7dce-400b-a4e0-c429a91a348f` |
| POS支付方式 | `pos_payment_method` | suggested | `dfe1bd74-7728-4b94-9187-e4c88ef93973` |
| POS收银员 | `pos_profile_user` | suggested | `4f988f0f-4ec7-46f4-8515-b47b12d24478` |
| 定价规则品牌 | `pricing_rule_brand` | suggested | `21d40c98-0cbd-4175-ba91-361981dfc98d` |
| 定价规则明细 | `pricing_rule_detail` | suggested | `107aa11a-4294-4f3e-a667-ee7e84dfcdfe` |
| 定价规则商品 | `pricing_rule_item_code` | suggested | `edc9f241-597e-4006-ab88-4203edb773cb` |
| 定价规则商品分类 | `pricing_rule_item_group` | suggested | `180add5f-5000-43ec-b953-ceb988b1226a` |
| 潜客线索 | `prospect_lead` | suggested | `707d5e87-0c45-437a-817c-7aeb29dc8ac7` |
| 潜客商机 | `prospect_opportunity` | suggested | `f1cc9e90-6d8e-4d5b-9cb6-43a49802aa03` |
| 报价明细 | `quotation_item` | suggested | `e20db5e4-5f7d-4f72-b712-af4521e857f8` |
| 报价流失原因明细 | `quotation_lost_reason_detail` | suggested | `df185aa8-4b20-4c86-a4b0-51dc6e72d129` |
| 询价单明细 | `request_for_quotation_item` | suggested | `09bd308f-77e9-46ba-8505-91c4691dcaa2` |
| 询价单供应商 | `request_for_quotation_supplier` | suggested | `9315e942-90f5-47f7-aaf2-e7fee83cc873` |
| 服务工作日 | `service_day` | suggested | `38e527e5-392a-45e6-996d-b0172f157eff` |
| 服务级别优先级 | `service_level_priority` | suggested | `ba28fe29-bcb3-49d5-87f9-c18ff88a80b5` |
| 发货交付明细 | `shipment_delivery_note` | suggested | `a30853ce-dfd3-4666-9ab0-1b1dab6fe8b6` |
| 发货包裹 | `shipment_parcel` | suggested | `675a9843-4f87-4bda-85af-a226cdf3854d` |
| 运费规则条件 | `shipping_rule_condition` | suggested | `d1671cd0-7d24-4ff7-aa3e-f0a2e9de8451` |
| 运费规则国家 | `shipping_rule_country` | suggested | `93430b0c-6e69-4b6b-a1cc-a60ffdcce65d` |
| 任务依赖 | `task_dependency` | suggested | `00fe113b-b25c-44ba-b584-e9afc827088f` |
| 税模板明细 | `tax_template_detail` | suggested | `02e67c97-5602-4bc2-9029-8ddd5e9816a9` |
| 代扣税账户 | `tax_withholding_account` | suggested | `421266b0-16e6-4561-88b9-6e00baa3717c` |
| 代扣税记录 | `tax_withholding_entry` | suggested | `ea142ae1-4daa-4639-a3bd-a5f5ab15cb65` |
| 代扣税率 | `tax_withholding_rate` | suggested | `5ffd73a5-1169-4c51-b72f-31a2bfa64ed6` |
| 区域明细 | `territory_item` | suggested | `ec64b2a5-78d3-4ad7-9d36-7f5c99f59e8f` |
| 保修索赔 | `warranty_claim` | suggested | `6e7a2544-07c8-46c7-8a0c-d7e46f9be043` |

## 生产制造（52）

| 显示名 | 标识名 | 状态 | ID |
|---|---|---|---|
| 资产维修 | `asset_repair` | suggested | `b2686eb9-eccc-47d7-afa9-ffda8390026f` |
| BOM创建单明细 | `bom_creator_item` | suggested | `1a1110ec-365b-416a-a536-b9920031e15f` |
| BOM展开明细 | `bom_explosion_item` | suggested | `92386de2-3e62-4c95-a3e2-5efd4917ed8a` |
| BOM明细行 | `bom_item` | suggested | `820297be-095a-4b98-bf54-124aafc0897f` |
| BOM工序 | `bom_operation` | suggested | `3b4cac3c-2f9c-4bdc-a6bc-45a4a3c8f6c4` |
| BOM副料 | `bom_secondary_item` | suggested | `6b2b46df-53f3-45bf-b107-4248849e14ea` |
| BOM更新日志 | `bom_update_log` | suggested | `9a48d28d-dd86-4063-9dec-e5d76cba6ee5` |
| BOM网站物料 | `bom_website_item` | suggested | `9619814c-23e0-4fc5-89de-8c35d4afb653` |
| BOM网站工序 | `bom_website_operation` | suggested | `eb9c30b9-e9e2-4ee3-9f5e-dea412e6f0a5` |
| 停机记录 | `downtime_entry` | published | `fe66cbe2-efeb-4529-bd9c-90c778cddce4` |
| 作业工单卡 | `job_card` | suggested | `070ba71e-e4ee-44ca-a71c-9342c04a8646` |
| 工单物料明细 | `job_card_item` | suggested | `5be38e28-1c03-4be1-aab6-32174b94cc73` |
| 工单工序 | `job_card_operation` | suggested | `d5e96ac8-e1e0-44b0-bdd3-454c9d75ac52` |
| 工单排程时间 | `job_card_scheduled_time` | suggested | `0c1cb9e1-df54-457d-bdc5-35b40669e7da` |
| 工单副料明细 | `job_card_secondary_item` | suggested | `25644b61-fabb-492f-8b5f-4079df0f1d77` |
| 工单工时记录 | `job_card_time_log` | suggested | `d008a1c0-72d0-424f-aa5e-840f766eaf83` |
| 请购单明细 | `material_request_item` | suggested | `dad6d178-036a-4996-8239-29e6481a302b` |
| 请购计划明细 | `material_request_plan_item` | suggested | `4c4c6b3d-2ba9-4017-bf80-89b437cc8c63` |
| 拣货明细 | `pick_list_item` | suggested | `80d88874-6cee-4539-b21b-f87aee156445` |
| 生产计划明细 | `production_plan_item` | suggested | `946b52fc-2232-4695-8657-d085a95db124` |
| 生产计划物料引用 | `production_plan_item_reference` | suggested | `e07db67f-e0bc-47ac-bb65-fb49e86047ea` |
| 生产计划领料单 | `production_plan_material_request` | suggested | `d03574bb-1f84-47cf-a1b6-ffd62640bbfb` |
| 生产计划领料仓库 | `production_plan_material_request_warehouse` | suggested | `bc2e07b6-17bb-400e-97a7-ce49f17defae` |
| 生产计划销售订单 | `production_plan_sales_order` | suggested | `dc4361d1-8a14-4a74-bea0-c12a7b91e305` |
| 生产计划组件明细 | `production_plan_sub_assembly_item` | suggested | `be8db38f-f987-44b6-a0c8-edcd6dc55914` |
| 质量行动 | `quality_action` | suggested | `c4c1d988-d26f-409c-b34b-facab05e5c5c` |
| 质量行动处理 | `quality_action_resolution` | suggested | `2b4d7a15-aa0c-41c5-a295-acde76f7e3b5` |
| 质量反馈 | `quality_feedback` | suggested | `0818635d-f36a-4c39-81eb-e95529793846` |
| 质量反馈评分 | `quality_feedback_parameter` | suggested | `797acdd7-f413-4c74-b2e8-0a6bb4390c71` |
| 质量反馈模板参数 | `quality_feedback_template_parameter` | suggested | `d27e8f5a-e6c0-4a2b-9b2c-ccb10e12ffd3` |
| 质量目标明细 | `quality_goal_objective` | suggested | `38a99db6-c64a-4878-8c1f-9508e516fb8c` |
| 质量检验 | `quality_inspection` | suggested | `be33dc73-95d1-422d-bc79-e122f7c5803f` |
| 检验读数明细 | `quality_inspection_reading` | suggested | `d1561e5b-6d3f-408e-bbbe-1f5b1a257a25` |
| 质量会议议程 | `quality_meeting_agenda` | suggested | `8e0b4098-9803-4716-9294-56c1ade591d5` |
| 质量会议纪要 | `quality_meeting_minutes` | suggested | `cdebdfc6-47fa-4892-8026-3e8bd4b22c05` |
| 质量程序流程 | `quality_procedure_process` | suggested | `8fe42bd3-6c91-4c67-bdf7-6d452a5e53f2` |
| 质量评审 | `quality_review` | suggested | `b1ee4165-1c02-4892-816a-346866bde108` |
| 质量评审目标 | `quality_review_objective` | suggested | `a1eaa287-3ee4-4573-b8b7-36ad037a7c96` |
| 库存异动单 | `stock_entry` | suggested | `5a2e931f-a5ef-482f-8310-2dac757a900d` |
| 委外加工内向订单明细 | `subcontracting_inward_order_item` | suggested | `0dce42e7-519a-4cd5-b235-3d1f3515fce0` |
| 委外加工内向订单收货明细 | `subcontracting_inward_order_received_item` | suggested | `d661cbc7-7471-48d3-b76b-f2fd8f6dbce5` |
| 委外加工内向订单二级明细 | `subcontracting_inward_order_secondary_item` | suggested | `b1cfba50-07b8-4d1d-871a-4a2ce1a6ad30` |
| 委外加工内向订单服务明细 | `subcontracting_inward_order_service_item` | suggested | `04ecc92b-6a65-49d9-8f52-02f68f527239` |
| 委外订单明细 | `subcontracting_order_item` | suggested | `5e8fbf4e-b44a-439a-a105-a91837afb223` |
| 委外订单服务明细 | `subcontracting_order_service_item` | suggested | `f92c0278-163c-4235-a173-f9f4fb1a47a3` |
| 委外订单供料明细 | `subcontracting_order_supplied_item` | suggested | `2d365927-d055-42bc-8647-1b9f6a0b3194` |
| 委外收货 | `subcontracting_receipt` | suggested | `132d32ac-d783-4812-af43-b7a594f09622` |
| 委外加工收货明细 | `subcontracting_receipt_item` | suggested | `cd83db42-6c5b-47a4-b950-2a87c2a13a55` |
| 委外收货供应物料 | `subcontracting_receipt_supplied_item` | suggested | `660fe0b6-fb53-44fd-9d31-22c677228a49` |
| 工单明细 | `work_order_item` | suggested | `cdada32d-0727-4cfb-86ca-9a5f98c9b085` |
| 工单工序 | `work_order_operation` | suggested | `efa98646-4b89-4e0b-aa97-dc1718f18beb` |
| 工位工作时段 | `workstation_working_hour` | suggested | `fa476603-53b9-4dd4-9bd8-e10893fccf69` |

## 资金与支付（44）

| 显示名 | 标识名 | 状态 | ID |
|---|---|---|---|
| 作业成本费率 | `activity_cost` | edited | `04ab049e-aa6e-4ccd-9dc0-b59cfe9e7a48` |
| 银行账户余额 | `bank_account_balance` | suggested | `0d4c58f6-8c5c-471e-bde7-5952ffc37803` |
| 银行流水导入 | `bank_statement_import` | suggested | `8757d46b-0cab-4dd5-8fd8-2cc3a89e7c27` |
| 银行交易规则 | `bank_transaction_rule` | edited | `aa944a60-c51f-4e66-903c-bf2023fb5280` |
| 员工教育经历 | `employee_education` | suggested | `9a913ceb-3bdb-41da-9c55-012ab24507c3` |
| 员工外部工作经历 | `employee_external_work_history` | suggested | `e90e0182-29bb-48a7-a13a-4ff1dcf1a7b9` |
| 员工分组成员 | `employee_group_member` | suggested | `9ea5f0ad-b8f9-40f2-bff2-0928d9c0dfda` |
| 员工内部工作经历 | `employee_internal_work_history` | suggested | `f6896d53-ae95-47f3-b3b7-f56a51a1444a` |
| 发票贴现 | `invoice_discounting` | suggested | `c052c8db-d56a-4bb1-a02d-8cd3348ce335` |
| 往来单位账户 | `party_account` | suggested | `7b1a441e-9bfd-42a6-97f0-458eb0dc6d8a` |
| 支付单 | `payment_entry` | suggested | `64cc01bc-4980-4ce4-9f99-9505b8ea166e` |
| 付款扣减明细 | `payment_entry_deduction` | suggested | `3cd143e6-eb3b-4d03-8e12-6c46f0bbc0a4` |
| 付款核销明细 | `payment_entry_reference` | suggested | `5fd291b2-f6b6-4396-bb49-dfe5d91373c9` |
| 支付台账 | `payment_ledger_entry` | edited | `5dfe6f63-9423-43f4-ae05-e1eb2d28fb89` |
| 付款单引用明细 | `payment_order_reference` | suggested | `7f2fb488-dcea-47cb-909c-9855244714ed` |
| 付款核销 | `payment_reconciliation` | suggested | `24dc362f-9024-4d4a-b42b-227fed88eb95` |
| 付款对账分摊明细 | `payment_reconciliation_allocation` | suggested | `87d6cdad-1ecc-4a6b-9474-2fe33373f2ec` |
| 付款对账日志 | `payment_reconciliation_log` | suggested | `4540528e-ca0a-4ae5-bfcd-960fa59a9bec` |
| 付款引用明细 | `payment_reference` | suggested | `6ddea822-bb88-420c-b3f4-8b7a98ce90df` |
| 付款计划 | `payment_schedule` | suggested | `467996a5-6366-4cd6-b475-0a6751fa5615` |
| 付款条件模板明细 | `payment_terms_template_detail` | suggested | `71af04f3-0b3a-435a-97c2-9aac50bdf6bb` |
| 销售预测明细 | `sales_forecast_item` | suggested | `0a0f322d-f654-438b-b7f6-610aeec7e4c3` |
| 销售发票 | `sales_invoice` | suggested | `659c02b5-91e0-47e4-ac7a-af8e31fba928` |
| 销售发票预付明细 | `sales_invoice_advance` | suggested | `f247c56b-b028-402d-9be5-3138b77e6b37` |
| 销售发票明细 | `sales_invoice_item` | suggested | `7a806d7c-4d36-42a8-a3d2-f689c8f9a9db` |
| 销售发票付款明细 | `sales_invoice_payment` | suggested | `a9fa7426-1920-46fa-b7d7-19f5960c0b4b` |
| 销售发票引用 | `sales_invoice_reference` | suggested | `70a6005d-54e6-4a13-ba86-c2fa14cc5a85` |
| 销售发票工时明细 | `sales_invoice_timesheet` | suggested | `677eada4-3f68-48f9-b2c1-e57a0b7b30f5` |
| 销售订单 | `sales_order` | suggested | `5bf6ff57-ab08-4b73-a0f4-a7c6cffe663e` |
| 销售订单明细 | `sales_order_item` | suggested | `f446a587-be05-407a-bb62-7715876292b7` |
| 销售合作伙伴佣金明细 | `sales_partner_item` | suggested | `ebefdc38-aac9-44f0-bd5e-d0788b3006a3` |
| 销售税费明细 | `sales_taxes_and_charges` | suggested | `cb7dcfbc-787d-4fbf-9d08-85d24d0514ed` |
| 销售团队分配 | `sales_team` | suggested | `c82f4eb8-a819-41b1-86d1-0d78615c63a7` |
| 订阅发票 | `subscription_invoice` | suggested | `e0ab9134-c3da-4da5-8ca4-4fa9708f7917` |
| 订阅计划明细 | `subscription_plan_detail` | suggested | `53a7c562-1122-436c-880f-b40749e45bb9` |
| 供应商分组明细 | `supplier_group_item` | suggested | `b7939a1d-f213-4632-ba8f-472f09766cc7` |
| 供应商物料 | `supplier_item` | suggested | `b5fd25ee-49b7-4602-a9fa-9d21f0860465` |
| 供应商客户编号 | `supplier_number_at_customer` | suggested | `312936f1-0e1d-4058-855c-47847454aad4` |
| 供应商报价明细 | `supplier_quotation_item` | suggested | `ca3f3b3d-b213-421d-95cb-5c7cad815b25` |
| 供应商记分卡评分标准 | `supplier_scorecard_scoring_criteria` | suggested | `85ba846b-93f5-4196-8433-c3ad59637757` |
| 供应商记分卡评分等级区间 | `supplier_scorecard_scoring_standing` | suggested | `fec0e961-3781-42ab-a12a-9d4ff141cc44` |
| 供应商记分卡评分变量 | `supplier_scorecard_scoring_variable` | suggested | `717fbf5a-d45b-4aa9-aa0e-406883df04a5` |
| 工时单 | `timesheet` | suggested | `ae964d07-09ed-4dfe-91fe-91869e089ec2` |
| 工时明细 | `timesheet_detail` | suggested | `e50d5df6-2730-468f-8349-768abf68e6ce` |

## 物料与库存管理（31）

| 显示名 | 标识名 | 状态 | ID |
|---|---|---|---|
| 送货单明细 | `delivery_note_item` | suggested | `1dfe9d02-f314-4580-9053-4d812bb09507` |
| 交付排程明细 | `delivery_schedule_item` | suggested | `5ebdc1d2-31a3-489e-97c1-0799ca8c69b9` |
| 交付停靠站 | `delivery_stop` | suggested | `85ebb4a4-24e3-45f9-a708-d2b7042a22b1` |
| 交付行程 | `delivery_trip` | suggested | `affb1026-b14c-48fd-85cb-d2019483f0b0` |
| 事件通知 | `event_notification` | suggested | `cd95064a-e1d9-4af6-adfc-38582bd3c677` |
| 事件参与者 | `event_participant` | suggested | `5bba4a44-fcba-4af8-af65-67ba936dd5c4` |
| 会计年度公司 | `fiscal_year_company` | suggested | `09a2fd2a-0de0-44e6-9b55-de2ac1bed049` |
| 总账分录 | `gl_entry` | suggested | `7101af54-8dbd-4be4-b078-85728b002acc` |
| 物料属性值 | `item_attribute_value` | suggested | `30734a8e-82a8-4820-a3cd-f805ee9c5a71` |
| 物料条码 | `item_barcode` | suggested | `1718f158-e2ab-42b3-b23c-2ecc8d3f626a` |
| 物料客户明细 | `item_customer_detail` | suggested | `7eb8c9f5-6bbc-45e4-bb39-4ba157ce3dbb` |
| 物料默认设置 | `item_default` | suggested | `5fb66cda-de70-4849-a197-5d43bb098ede` |
| 质检参数 | `item_quality_inspection_parameter` | suggested | `b94b2501-a8ff-4ba5-bcf8-449803074700` |
| 物料供应商 | `item_supplier` | suggested | `c11f7e19-7983-4222-91b0-fdf824becece` |
| 物料税务 | `item_tax` | suggested | `54048e78-c713-46dc-b22d-b8c27fa570b6` |
| 商品税务明细 | `item_tax_detail` | suggested | `303c9d3f-ddd6-4c54-bc63-2f4a27b61059` |
| 物料变体 | `item_variant` | suggested | `5b94882d-8306-4095-9723-500475e489b6` |
| 商品网站规格 | `item_website_specification` | suggested | `79bd980a-d622-4982-98b3-3b5738aae2ce` |
| 装箱单明细 | `packing_slip_item` | suggested | `f8a633ef-092e-459e-aeb6-1a147eeaa8fd` |
| 促销价格折扣明细 | `promotional_scheme_price_discount` | suggested | `99984056-8a49-465b-a61a-3cff0d88326d` |
| 促销赠品明细 | `promotional_scheme_product_discount` | suggested | `5749dbc7-b78c-4021-9a8d-1f5d8f31de8f` |
| 序列号批次捆绑 | `serial_batch_bundle` | suggested | `c8cffc25-bb76-4be1-9805-e7a4dacddf29` |
| 序列号批次明细 | `serial_batch_entry` | suggested | `022628b9-de95-4a41-abc1-806d68d74338` |
| 库存期末余额 | `stock_closing_balance` | suggested | `9a91fa6e-9e86-4a7e-bd16-505d8e98ca93` |
| 库存结账 | `stock_closing_entry` | published | `720b268a-bdf3-4e97-81a2-b414093941e7` |
| 库存出入库明细 | `stock_entry_detail` | suggested | `6a83328f-c9f3-4625-b16e-acb606b03660` |
| 库存台账流水 | `stock_ledger_entry` | suggested | `f728b390-7943-432d-902a-00ab16123029` |
| 库存对账 | `stock_reconciliation` | suggested | `6d74698e-f6eb-428b-8557-250ba269f017` |
| 库存对账明细 | `stock_reconciliation_item` | suggested | `2a56b1c1-da2f-409f-8945-824982a8d314` |
| 库存预留记录 | `stock_reservation_entry` | suggested | `606ac52f-25d0-4585-9128-ab2850a74fff` |
| 计量单位换算明细 | `uom_conversion_detail` | suggested | `d029f504-a116-4f6c-9791-310e2de475f1` |

## 公共主数据（22）

| 显示名 | 标识名 | 状态 | ID |
|---|---|---|---|
| 公司发展历程 | `company_history` | suggested | `08b279ca-f077-48a0-86cf-5b202d6b1055` |
| 联系人邮箱 | `contact_email` | suggested | `9f3b6b07-4fbc-48d6-98a1-87859d3f6dba` |
| 联系人电话 | `contact_phone` | suggested | `1ee4bbbe-9953-4bf9-9a67-a0611400f3ec` |
| 成本中心分摊比例 | `cost_center_allocation_percentage` | suggested | `1da59feb-dffd-496b-9e8b-579160f2e600` |
| 汇率设置明细 | `currency_exchange_settings_details` | suggested | `397c61a7-e1f0-4b71-8072-fbdcd6f718fb` |
| 汇率设置结果 | `currency_exchange_settings_result` | suggested | `706a392e-f97c-44d9-96aa-1afffa1bc111` |
| 汇率重估 | `exchange_rate_revaluation` | published | `72db6a6e-5ad8-4927-bdcd-af232d4266c7` |
| 汇率重估账户 | `exchange_rate_revaluation_account` | suggested | `0268e764-2b39-4ba5-821d-c2115ed8d6eb` |
| 替代物料 | `item_alternative` | published | `74efaaf3-1497-4a2f-86d5-5aef7743a719` |
| 账本健康检查 | `ledger_health` | suggested | `b43f9b68-5a1d-4a34-a27e-e0cb8064c8c6` |
| 账本健康监控公司 | `ledger_health_monitor_company` | suggested | `da45f3e2-074a-427c-958c-9e4b5f38f694` |
| 账本合并 | `ledger_merge` | suggested | `325a904d-c196-4203-944b-4b2d622b6efc` |
| 账本合并账户 | `ledger_merge_accounts` | suggested | `9fc213ca-1613-4c10-ba16-1caafeeef96a` |
| 项目更新 | `project_update` | published | `942583cf-afc6-40cc-8ef5-b712f700e4d9` |
| 重记账分类账 | `repost_accounting_ledger` | published | `da41b286-6040-46f8-ab66-8bb82e0272e8` |
| 重记账凭证明细 | `repost_accounting_ledger_item` | suggested | `6543fa06-8331-4dad-b3b3-2609b35b56bf` |
| 物料估值重算 | `repost_item_valuation` | suggested | `c7955cc5-44e6-4a02-aadf-5312a2a7f270` |
| 重算付款台账 | `repost_payment_ledger` | suggested | `0ccb40ea-5b16-4c2f-ae82-de0f9eaa2e6b` |
| 重算付款台账明细 | `repost_payment_ledger_item` | suggested | `50544511-7b79-4480-a15e-903ac9d05b6a` |
| 供应商绩效评估周期 | `supplier_scorecard_period` | published | `2fce465d-7098-401e-a4f9-cccb4cc6f1c9` |
| 解除核销付款 | `unreconcile_payment` | suggested | `beac66d6-fc6a-4100-b07d-1cefff0be328` |
| 解除核销付款明细 | `unreconcile_payment_entry` | suggested | `f4dfd955-513b-40fe-a062-1bb9a3fcd01f` |

## 业财管理（19）

| 显示名 | 标识名 | 状态 | ID |
|---|---|---|---|
| 资产活动 | `asset_activity` | edited | `94b405c6-831c-43dd-a92e-3ece8c2228f7` |
| 通话记录 | `call_log` | edited | `99ccdcfb-c589-49fb-b987-a3a7205be751` |
| 收银交接 | `cashier_closing` | published | `136c08a2-c055-4cb7-9340-a62044033543` |
| 收银交接支付明细 | `cashier_closing_payment` | suggested | `12740c3e-5ff7-4211-b82e-3250d6a91906` |
| 沟通记录 | `communication` | edited | `afd2a600-fd60-4e52-a590-39bfc542a063` |
| 沟通关联 | `communication_link` | suggested | `d82f09c2-b68e-45f0-b485-8618561516c5` |
| 渠道时段 | `communication_medium_timeslot` | suggested | `8a1e5250-383b-4afb-ba42-cf9f21a02649` |
| 合同履行检查清单 | `contract_fulfilment_checklist` | suggested | `cdf38665-dbb3-4ed3-a198-94d72ef62c53` |
| 合同模板履行条款 | `contract_template_fulfilment_terms` | suggested | `e7f25550-c50a-4092-a119-0e614b27f599` |
| 递延会计处理 | `deferred_accounting` | edited | `fc56f60d-cce6-444f-b2e6-8a7a387a18f5` |
| 会计凭证 | `journal_entry` | edited | `68a1ac2f-b87b-44d0-a6fe-2bec48dcd5db` |
| 分录科目明细 | `journal_entry_account` | suggested | `dee81c09-0665-4429-a586-79ae52faaeb8` |
| 分录模板科目明细 | `journal_entry_template_account` | suggested | `631514f2-9b18-4ece-bf0f-d7cf82312f0a` |
| 支付方式账户 | `mode_of_payment_account` | suggested | `3ca9d586-d486-4dc1-ac67-f1966d916a74` |
| 个人数据删除申请 | `personal_data_deletion_request` | suggested | `df351a65-9bd0-4196-9362-d9fac22ccb1d` |
| 个人数据删除步骤 | `personal_data_deletion_step` | suggested | `ebe82122-1415-4ef4-8341-a0684f73b68c` |
| 个人数据下载申请 | `personal_data_download_request` | published | `83e9d3b8-bbae-4ed2-87f9-5b1c1cf1d65f` |
| 用户组成员 | `user_group_member` | suggested | `4bf1401d-de2c-41cd-96c3-1ad3e2831d77` |
| 用户邀请 | `user_invitation` | published | `32d8bb51-4a97-4f26-88b1-ebfe601f9644` |

## 资产管理（16）

| 显示名 | 标识名 | 状态 | ID |
|---|---|---|---|
| 账户期末余额 | `account_closing_balance` | suggested | `43040b81-b843-401c-a4af-01e50be5e5e2` |
| 资产资本化 | `asset_capitalization` | suggested | `cb956afa-e6a8-43c3-b3d8-460a872012fa` |
| 资本化资产明细 | `asset_capitalization_asset_item` | edited | `fb95ff0f-cbc5-40a2-92df-e9dcc226881a` |
| 资本化服务明细 | `asset_capitalization_service_item` | edited | `f9c015b3-6d56-4332-aea6-a3c3f1b30ef4` |
| 资本化库存明细 | `asset_capitalization_stock_item` | edited | `006fd43b-acb0-4cf7-a533-9554146f0c0b` |
| 资产分类科目配置 | `asset_category_account` | edited | `725cbe43-064e-4d83-8aaf-177345c1524b` |
| 资产财务账簿 | `asset_finance_book` | edited | `601c3d68-3279-40a5-bad0-584b487e6ad3` |
| 资产维护 | `asset_maintenance` | suggested | `c3815980-020b-4c36-a84c-5200a1511595` |
| 资产维护记录 | `asset_maintenance_log` | suggested | `3b9292d6-b7c8-46db-87f7-084c49ec4cc2` |
| 资产维护任务 | `asset_maintenance_task` | edited | `955b842f-70e1-498d-bbbe-d212d5b9d9b5` |
| 资产移动 | `asset_movement` | edited | `45c76d30-ab6b-472b-a449-8fc628d9b35b` |
| 资产移动明细 | `asset_movement_item` | edited | `eb1fd818-74e8-4b3a-a759-a8b807734e47` |
| 资产维修消耗物料 | `asset_repair_consumed_item` | edited | `aa5b40ea-de27-45c5-9db8-a3c8f586edd2` |
| 资产维修采购发票 | `asset_repair_purchase_invoice` | edited | `e8fe2c2f-57df-4ea6-893b-9e226312639c` |
| 资产班次分配 | `asset_shift_allocation` | edited | `4e93917b-69c9-47ee-ad47-4d03c0c464a2` |
| 资产价值调整 | `asset_value_adjustment` | edited | `50985f6f-8792-46da-af44-332e5514f2e7` |

## 账款催收（2）

| 显示名 | 标识名 | 状态 | ID |
|---|---|---|---|
| 催款 | `dunning` | suggested | `91708a46-9362-455a-b491-79cd08e34a1a` |
| 催款信函文本 | `dunning_letter_text` | suggested | `454db783-746e-449e-9c62-e3e2a03e020b` |

## 订阅定价（1）

| 显示名 | 标识名 | 状态 | ID |
|---|---|---|---|
| 价格表国家 | `price_list_country` | suggested | `00d43884-7e7c-44c5-ba41-8e7dd65b4826` |

## 项目分类（2）

| 显示名 | 标识名 | 状态 | ID |
|---|---|---|---|
| 项目模板任务 | `project_template_task` | suggested | `c01c8be0-a4db-4641-b6d6-879b323268e1` |
| 项目用户 | `project_user` | suggested | `f17dd299-6f70-45c4-85c4-fc89b6f91bea` |
