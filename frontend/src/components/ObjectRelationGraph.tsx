import { memo, useMemo } from "react";
import { OntologyGraphView } from "./graph";
import type { ObjectTypeDetail, OntologyGraph } from "../types";

interface Props {
  obj: ObjectTypeDetail;
  objectDetailPath?: (objectId: string) => string;
  relationDetailPath?: (relationId: string) => string;
  onEdgeClick?: (relationId: string, sourceObjectId: string) => void;
  height?: number;
  embedded?: boolean;
}

function ObjectRelationGraphInner({
  obj,
  objectDetailPath,
  relationDetailPath,
  onEdgeClick,
  height = 480,
  embedded = false,
}: Props) {
  const graph = useMemo(() => buildRelationGraph(obj), [obj]);

  const handleEdgeClick = (edge: OntologyGraph["edges"][number]) => {
    if (onEdgeClick) {
      const relationId = edge.relationId || edge.relation_id || edge.id.replace(/^in-/, "");
      onEdgeClick(relationId, edge.source);
    }
  };

  return (
    <OntologyGraphView
      graph={graph}
      centerNodeId={obj.id}
      objectDetailPath={objectDetailPath}
      relationDetailPath={relationDetailPath}
      onEdgeClick={onEdgeClick ? handleEdgeClick : undefined}
      height={height}
      embedded={embedded}
    />
  );
}

export const ObjectRelationGraph = memo(ObjectRelationGraphInner);

function buildRelationGraph(obj: ObjectTypeDetail): OntologyGraph {
  const nodeMap = new Map<string, OntologyGraph["nodes"][number]>();

  nodeMap.set(obj.id, {
    id: obj.id,
    label: obj.name,
    display_name: obj.display_name,
    status: obj.status,
  });

  const edges: OntologyGraph["edges"] = [];

  for (const rel of obj.outgoing_relations) {
    if (!nodeMap.has(rel.target_object_type_id)) {
      nodeMap.set(rel.target_object_type_id, {
        id: rel.target_object_type_id,
        label: rel.target_object_name || rel.target_object_type_id,
        display_name: rel.target_object_name || rel.target_object_type_id,
        status: rel.status,
      });
    }
    edges.push({
      id: rel.id,
      relationId: rel.id,
      source: obj.id,
      target: rel.target_object_type_id,
      label: rel.display_name,
      cardinality: rel.cardinality,
    });
  }

  for (const rel of obj.incoming_relations) {
    if (!nodeMap.has(rel.source_object_type_id)) {
      nodeMap.set(rel.source_object_type_id, {
        id: rel.source_object_type_id,
        label: rel.source_object_name || rel.source_object_type_id,
        display_name: rel.source_object_name || rel.source_object_type_id,
        status: rel.status,
      });
    }
    edges.push({
      id: `in-${rel.id}`,
      relationId: rel.id,
      source: rel.source_object_type_id,
      target: obj.id,
      label: rel.display_name,
      cardinality: rel.cardinality,
    });
  }

  return {
    nodes: Array.from(nodeMap.values()),
    edges,
  };
}
