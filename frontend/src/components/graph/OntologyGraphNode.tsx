import { Handle, Position, type NodeProps } from "@xyflow/react";
import { StatusBadge } from "../StatusBadge";

export interface OntologyNodeData extends Record<string, unknown> {
  label: string;
  status: string;
  isCenter?: boolean;
}

export function OntologyGraphNode({ data }: NodeProps) {
  const nodeData = data as OntologyNodeData;

  return (
    <div className={`ontology-flow-node${nodeData.isCenter ? " ontology-flow-node--center" : ""}`}>
      <Handle type="target" position={Position.Top} className="ontology-flow-handle" />
      <div className="ontology-flow-node-title" title={nodeData.label}>
        {nodeData.label}
      </div>
      <StatusBadge status={nodeData.status} />
      <Handle type="source" position={Position.Bottom} className="ontology-flow-handle" />
    </div>
  );
}
