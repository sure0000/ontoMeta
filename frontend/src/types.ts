export interface DomainContext {
  id: string;
  datahub_domain_id: string;
  name: string;
  description?: string;
  owner?: string;
  status: string;
  draft_count: number;
  published_count: number;
  latest_draft_at?: string;
  latest_published_at?: string;
  updated_at: string;
}

export interface DomainContextDetail extends DomainContext {
  datahub_url?: string;
  latest_ontology_id?: string;
  latest_ontology_status?: string;
  published_ontology_id?: string;
  published_ontology_version?: number;
}

export interface DraftProgress {
  task_id: string;
  status: string;
  progress: number;
  message?: string;
  ontology_id?: string;
}

export interface TaskRecord {
  id: string;
  status: string;
  progress: number;
  message?: string;
  ontology_id?: string;
  created_at: string;
  updated_at: string;
}

export interface ChangeLog {
  id: string;
  entity_type: string;
  entity_id: string;
  action: string;
  operator?: string;
  change_summary?: string;
  created_at: string;
}

export interface ObjectTypeSummary {
  id: string;
  name: string;
  display_name: string;
  description?: string;
  status: string;
  property_count: number;
  relation_count: number;
  business_logic_count: number;
  bound_logic_count?: number;
  source_confidence?: number;
  updated_at: string;
}

export interface Property {
  id: string;
  name: string;
  display_name: string;
  description?: string;
  data_type?: string;
  semantic_type?: string;
  source_field_ref?: string;
  required: boolean;
  source_confidence?: number;
  status: string;
}

export interface RelationType {
  id: string;
  name: string;
  display_name: string;
  description?: string;
  source_object_type_id: string;
  target_object_type_id: string;
  source_object_name?: string;
  target_object_name?: string;
  cardinality?: string;
  structure_type?: string;
  mapping_object_type_id?: string | null;
  mapping_object_name?: string | null;
  source_evidence?: string;
  status: string;
  source_confidence?: number;
}

export interface RelationObjectRef {
  id: string;
  name: string;
  display_name: string;
  source_ref?: string;
  datahub_url?: string;
}

export interface DataHubDatasetOption {
  urn: string;
  name: string;
  display_name?: string;
  description?: string;
  platform?: string;
  container?: string;
  object_type_id?: string | null;
  object_type_display_name?: string | null;
  datahub_url?: string;
}

export interface RelationTypeDetail extends RelationType {
  ontology_id: string;
  source_evidence?: string;
  source_object?: RelationObjectRef;
  target_object?: RelationObjectRef;
  mapping_object?: RelationObjectRef | null;
}

export interface ObjectTypeDetail extends ObjectTypeSummary {
  ontology_id?: string;
  domain_context_id?: string;
  domain_name?: string;
  source_ref?: string;
  datahub_url?: string;
  properties: Property[];
  outgoing_relations: RelationType[];
  incoming_relations: RelationType[];
  business_logics: BusinessLogic[];
  business_logic_bindings?: ObjectTypeLogicBinding[];
  version_records?: VersionRecord[];
}

export interface BusinessLogic {
  id: string;
  name: string;
  display_name: string;
  logic_type: string;
  description?: string;
  expression_summary?: string;
  source_type?: string;
  source_ref?: string;
  status: string;
  source_confidence?: number;
  domain_context_id?: string;
  domain_name?: string;
  bound_object_count?: number;
  bound_property_count?: number;
  updated_at: string;
}

export interface ObjectTypeLogicBinding {
  binding_id: string;
  role: string;
  source: string;
  confidence?: number;
  logic_id: string;
  logic_name: string;
  logic_display_name: string;
  logic_type: string;
  logic_status: string;
  created_at: string;
}

export interface BusinessLogicObjectBinding {
  id: string;
  business_logic_id: string;
  object_type_id: string;
  object_type_name?: string;
  object_type_display_name?: string;
  role: string;
  source: string;
  confidence?: number;
  created_at: string;
}

export interface BusinessLogicPropertyBinding {
  id: string;
  business_logic_id: string;
  property_id: string;
  property_name?: string;
  property_display_name?: string;
  object_type_id?: string;
  object_type_name?: string;
  role: string;
  source: string;
  confidence?: number;
  created_at: string;
}

export interface BusinessLogicDetail extends BusinessLogic {
  related_object_types: ObjectTypeSummary[];
  related_properties?: Property[];
  object_bindings?: BusinessLogicObjectBinding[];
  property_bindings?: BusinessLogicPropertyBinding[];
  version_records?: VersionRecord[];
}

export interface VersionRecord {
  id: string;
  entity_type: string;
  entity_id: string;
  version: number;
  diff_summary?: string;
  operator?: string;
  created_at: string;
}

export interface OntologySummary {
  id: string;
  domain_context_id: string;
  version: number;
  status: string;
  generated_at?: string;
  published_at?: string;
  object_type_count: number;
  relation_type_count: number;
  business_logic_count: number;
}

export interface GraphNode {
  id: string;
  label: string;
  display_name: string;
  status: string;
}

export interface GraphEdge {
  id: string;
  source: string;
  target: string;
  label: string;
  cardinality?: string;
  relationId?: string;
  relation_id?: string;
}

export interface OntologyGraph {
  nodes: GraphNode[];
  edges: GraphEdge[];
}

export interface Confirmation {
  id: string;
  ontology_id: string;
  target_type: string;
  target_id?: string;
  action_type: string;
  confirmation_status: string;
  operator?: string;
  reason?: string;
  confirmed_at?: string;
  created_at: string;
}

export interface LlmModelOption {
  id: string;
  label: string;
  description: string;
  deprecated?: boolean;
}

export interface LlmServiceConfig {
  id: string;
  name: string;
  provider: string;
  api_base_url: string;
  model: string;
  is_default: boolean;
  enabled: boolean;
  use_mock: boolean;
  api_key_set: boolean;
  api_key_hint?: string;
  api_key?: string;
  created_at: string;
  updated_at: string;
}

export interface DatahubSettings {
  gms_url: string;
  frontend_url: string;
  token_set: boolean;
  token_hint?: string;
  use_mock: boolean;
  updated_at: string;
}
