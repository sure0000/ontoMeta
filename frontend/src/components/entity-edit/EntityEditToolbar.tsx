import { SaveOutlined, SendOutlined } from "@ant-design/icons";
import { Button, Space } from "antd";
import type { ReactNode } from "react";

type Props = {
  saving?: boolean;
  prePublishing?: boolean;
  canPrePublish?: boolean;
  onSave: () => void;
  onPrePublish: () => void;
  /** 额外操作（如返回工作区） */
  leading?: ReactNode;
};

/** 工作区实体详情页共用的「保存 / 预发布」操作条。 */
export function EntityEditToolbar({
  saving = false,
  prePublishing = false,
  canPrePublish = true,
  onSave,
  onPrePublish,
  leading,
}: Props) {
  return (
    <Space>
      {leading}
      <Button icon={<SaveOutlined />} loading={saving} onClick={onSave}>
        保存
      </Button>
      <Button
        type="primary"
        icon={<SendOutlined />}
        loading={prePublishing}
        disabled={!canPrePublish}
        onClick={onPrePublish}
      >
        预发布
      </Button>
    </Space>
  );
}
