/** Airflow 编排配置面板。
 *
 * **编排的全部可调项都在这里，不需要改任何配置文件**：怎么连 Airflow、DAG 往哪投、
 * 走哪条执行通道、分批与超时怎么定。环境变量只在首次建配置行时播一次种，此后以本页为准。
 *
 * 搬运工具也在这里（默认自动，见 services/sync_tool_resolver）——它是部署事实，不该在
 * 物化弹窗里逐次选。仍不在这里的：同步策略逐实体存在物化契约上；源库/目标库的凭据分别归 Airflow
 * Connection（docker 通道）与 sync-runner 的 secrets（runner 通道）——凭据只有一个
 * 归属地，ontoMeta 产出的 DAG 里只出现别名。
 */

import { useCallback, useEffect, useState } from "react";
import {
  Alert,
  Button,
  Collapse,
  Divider,
  Form,
  Input,
  InputNumber,
  Radio,
  Select,
  Space,
  Switch,
  Tag,
  message,
} from "antd";
import { ApiOutlined, ThunderboltOutlined } from "@ant-design/icons";
import { api } from "../api";
import type { AirflowSettings } from "../types";
import { SyncRunnerSecretsPanel } from "./SyncRunnerSecretsPanel";

interface FormValues {
  endpoint: string;
  username?: string;
  password?: string;
  api_version: string;
  enabled: boolean;
  dags_dir: string;
  jobs_dir: string;
  dag_delivery_method: string;
  git_remote: string;
  git_branch: string;
  git_auto_init: boolean;
  git_author: string;
  git_email: string;
  sync_channel: string;
  sync_runner_endpoint: string;
  docker_network: string;
  drivers_dir: string;
  sync_tool_images: string;
  sync_tool: string;
  max_tasks_per_dag: number;
  max_active_tasks_per_dag: number;
  dag_parse_timeout: number;
  preflight_sentinel_timeout: number;
  staging_swap: boolean;
  sync_runner_token?: string;
}

export function AirflowSettingsPanel() {
  const [form] = Form.useForm<FormValues>();
  const [settings, setSettings] = useState<AirflowSettings | null>(null);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);

  const load = useCallback(() => {
    api
      .getAirflowSettings()
      .then((s) => {
        setSettings(s);
        form.setFieldsValue({
          endpoint: s.endpoint,
          username: s.username ?? undefined,
          password: undefined, // 密文不回显，留空 = 保持不变
          api_version: s.api_version,
          enabled: s.enabled,
          dags_dir: s.dags_dir,
          jobs_dir: s.jobs_dir,
          dag_delivery_method: s.dag_delivery_method,
          git_remote: s.git_remote,
          git_branch: s.git_branch,
          git_auto_init: s.git_auto_init,
          git_author: s.git_author,
          git_email: s.git_email,
          sync_channel: s.sync_channel,
          sync_runner_endpoint: s.sync_runner_endpoint,
          docker_network: s.docker_network,
          drivers_dir: s.drivers_dir,
          sync_tool_images: s.sync_tool_images,
          sync_tool: s.sync_tool ?? "",
          max_tasks_per_dag: s.max_tasks_per_dag,
          max_active_tasks_per_dag: s.max_active_tasks_per_dag,
          dag_parse_timeout: s.dag_parse_timeout,
          preflight_sentinel_timeout: s.preflight_sentinel_timeout,
          staging_swap: s.staging_swap,
          sync_runner_token: undefined, // 密文不回显，留空 = 保持不变
        });
      })
      .catch((err: unknown) =>
        message.error(err instanceof Error ? err.message : "读取 Airflow 配置失败"),
      );
  }, [form]);

  useEffect(load, [load]);

  const save = async () => {
    let values: FormValues;
    try {
      values = await form.validateFields();
    } catch {
      return;
    }
    setSaving(true);
    try {
      const updated = await api.updateAirflowSettings({
        ...values,
        username: values.username?.trim() || null,
        password: values.password?.trim() || undefined,
        sync_runner_token: values.sync_runner_token?.trim() || undefined,
      });
      setSettings(updated);
      message.success("Airflow 配置已保存");
    } catch (err) {
      message.error(err instanceof Error ? err.message : "保存失败");
    } finally {
      setSaving(false);
    }
  };

  const test = async () => {
    setTesting(true);
    try {
      await api.testAirflowConnection();
      message.success("Airflow 连接正常");
    } catch (err) {
      message.error(err instanceof Error ? err.message : "连接失败");
    } finally {
      setTesting(false);
    }
  };

  return (
    <>
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 16 }}
        message={
          <span>
            物化执行：
            {settings?.available ? (
              <Tag color="success" style={{ marginLeft: 6 }}>
                可用（Airflow 编排）
              </Tag>
            ) : (
              <Tag color="warning" style={{ marginLeft: 6 }}>
                不可用（未启用 / 未配 endpoint）
              </Tag>
            )}
          </span>
        }
        description={
          settings?.available
            ? "物化将产出建表 DDL + 搬运作业 + DAG 交给 Airflow 执行，血缘由 DataHub 插件自动上报。搬运工具默认自动选（见下方「执行通道」），同步策略逐实体存在物化契约上。"
            : "物化一律交 Airflow 编排执行（已去除直连落库模式）：未启用或未配 endpoint 时，物化将报错无法执行。"
        }
      />
      <Form form={form} layout="vertical" style={{ maxWidth: 640 }}>
        <Form.Item
          label="启用调度执行"
          name="enabled"
          valuePropName="checked"
          extra="物化必须经 Airflow 执行；关闭后物化将不可用"
        >
          <Switch />
        </Form.Item>
        <Form.Item
          label="Airflow 地址"
          name="endpoint"
          rules={[{ required: true, message: "请输入 Airflow 地址" }]}
          extra="例如 http://localhost:8081"
        >
          <Input prefix={<ApiOutlined />} placeholder="http://localhost:8081" />
        </Form.Item>
        <Space align="start" wrap>
          <Form.Item label="账号" name="username">
            <Input placeholder="admin" style={{ width: 200 }} autoComplete="off" />
          </Form.Item>
          <Form.Item
            label="密码"
            name="password"
            extra={
              settings?.password_set
                ? `已配置：${settings.password_hint ?? "****"}，留空则保持不变`
                : undefined
            }
          >
            <Input.Password
              placeholder="Airflow 密码"
              style={{ width: 220 }}
              autoComplete="new-password"
            />
          </Form.Item>
          <Form.Item
            label="REST 版本"
            name="api_version"
            extra="Airflow 2.x=v1，3.x=v2"
          >
            <Input placeholder="v1" style={{ width: 100 }} />
          </Form.Item>
        </Space>
        <Collapse
          ghost
          items={[
            {
              key: "delivery",
              label: "DAG 投递",
              children: (
                <>
                  <Form.Item
                    label="投递方式"
                    name="dag_delivery_method"
                    extra="local：直接写本地目录（Airflow 与本服务同机或共享网络卷 NFS/SMB）；git：写完自动 commit + push 到远程仓，Airflow 侧 git-sync 拉取（跨机部署）"
                  >
                    <Radio.Group
                      optionType="button"
                      options={[
                        { label: "local（本地/共享卷）", value: "local" },
                        { label: "git（git-sync 跨机）", value: "git" },
                      ]}
                    />
                  </Form.Item>
                  <Form.Item
                    label="DAG 目录"
                    name="dags_dir"
                    extra="local 通道：必须是 Airflow 真正挂进容器的那个目录；git 通道：本地 git 工作副本里的 dags 目录（两侧不一致时提交前自检会验）"
                  >
                    <Input placeholder="/opt/airflow/dags 在宿主机上的路径" />
                  </Form.Item>
                  <Form.Item
                    label="作业配置目录"
                    name="jobs_dir"
                    extra="docker 通道的搬运作业配置落在这里，并挂进搬运容器；runner 通道不用"
                  >
                    <Input placeholder="…/seatunnel/jobs" />
                  </Form.Item>
                  <Form.Item
                    noStyle
                    shouldUpdate={(prev, cur) =>
                      prev.dag_delivery_method !== cur.dag_delivery_method
                    }
                  >
                    {({ getFieldValue }) =>
                      getFieldValue("dag_delivery_method") === "git" ? (
                        <>
                          <Alert
                            type="info"
                            showIcon
                            style={{ marginBottom: 12 }}
                            message="git-sync 前置条件"
                            description="DAG 目录须在一个已配好 remote 的 git 工作副本内，且本服务进程有推送凭据（SSH key / HTTPS token）。Airflow 侧用 git-sync sidecar（2.x）或内置 DAG bundle（3.x）拉取同一仓库。产物进 git 天然可 diff / review / 回滚。"
                          />
                          <Space align="start" wrap>
                            <Form.Item
                              label="remote 名称"
                              name="git_remote"
                              extra="默认 origin"
                            >
                              <Input placeholder="origin" style={{ width: 160 }} />
                            </Form.Item>
                            <Form.Item
                              label="推送分支"
                              name="git_branch"
                              extra="默认 main"
                            >
                              <Input placeholder="main" style={{ width: 160 }} />
                            </Form.Item>
                          </Space>
                          <Form.Item
                            label="目录不是 git 仓库时自动 init"
                            name="git_auto_init"
                            valuePropName="checked"
                            extra="关闭时若 DAG 目录不是 git 仓库则报错（提交前自检会验仓库与 remote 连通性）"
                          >
                            <Switch />
                          </Form.Item>
                          <Space align="start" wrap>
                            <Form.Item
                              label="commit 作者名"
                              name="git_author"
                              extra="留空则用 git 全局配置"
                            >
                              <Input placeholder="ontoMeta" style={{ width: 200 }} />
                            </Form.Item>
                            <Form.Item
                              label="commit 邮箱"
                              name="git_email"
                              extra="留空则用 git 全局配置"
                            >
                              <Input
                                placeholder="ontometa@example.com"
                                style={{ width: 240 }}
                              />
                            </Form.Item>
                          </Space>
                        </>
                      ) : null
                    }
                  </Form.Item>
                </>
              ),
            },
            {
              key: "channel",
              label: "执行通道",
              children: (
                <>
                  <Form.Item
                    label="通道"
                    name="sync_channel"
                    extra="runner：Airflow 任务向常驻 sync-runner 发 HTTP（推荐）；docker：经 docker.sock 起搬运容器，docker.sock 可达性/网络名/驱动挂载只有真起容器才知道成不成立"
                  >
                    <Radio.Group
                      optionType="button"
                      options={[
                        { label: "runner（常驻服务）", value: "runner" },
                        { label: "docker（兄弟容器）", value: "docker" },
                      ]}
                    />
                  </Form.Item>
                  <Form.Item
                    label="sync-runner 地址"
                    name="sync_runner_endpoint"
                    extra="ontoMeta 与 Airflow worker 用的是同一个值，故要填两边都能解析的地址（容器名在宿主机上解析不了，127.0.0.1 在 worker 里指向它自己）"
                  >
                    <Input placeholder="http://sync-runner:8098" />
                  </Form.Item>
                  <Form.Item
                    label="runner 访问令牌"
                    name="sync_runner_token"
                    extra={
                      settings?.sync_runner_token_set
                        ? "已配置，留空则保持不变。runner 侧需设同一个 SYNC_RUNNER_TOKEN"
                        : "runner 侧设了 SYNC_RUNNER_TOKEN 才需要；管理下方的连接配置必须有它"
                    }
                  >
                    <Input.Password
                      placeholder="与 runner 的 SYNC_RUNNER_TOKEN 一致"
                      autoComplete="new-password"
                    />
                  </Form.Item>
                  <Form.Item
                    label="搬运容器网络"
                    name="docker_network"
                    extra="仅 docker 通道：搬运容器要能解析源库/数仓的容器名，默认 bridge 做不到"
                  >
                    <Input placeholder="bridge" style={{ width: 260 }} />
                  </Form.Item>
                  <Form.Item
                    label="JDBC 驱动目录"
                    name="drivers_dir"
                    extra="仅 docker 通道：驱动因授权不随镜像分发，逐个 jar 挂进搬运容器"
                  >
                    <Input placeholder="…/seatunnel/drivers" />
                  </Form.Item>
                  <Form.Item
                    label="搬运工具镜像覆盖"
                    name="sync_tool_images"
                    extra="仅 docker 通道：工具名=镜像，逗号分隔。DataX 无官方镜像，不在这里指到自建镜像就不可选"
                  >
                    <Input placeholder="datax=registry.internal/datax:3.0" />
                  </Form.Item>
                  {/* 搬运工具的**唯一**人工入口：物化弹窗已不再逐次选（工具是部署事实，
                      且 runner 通道下不参与执行）。默认自动，需要钉住时才来这里改。 */}
                  <Form.Item
                    label="搬运工具"
                    name="sync_tool"
                    extra="留空 = 自动：runner 通道由执行侧逐表自选档位，docker 通道按「装载方式 ∩ 镜像可用」挑。指定后物化一律用它（该工具搬不了的表会列进未生成作业）"
                  >
                    <Select
                      style={{ width: 260 }}
                      options={[
                        { value: "", label: "自动（推荐）" },
                        { value: "seatunnel", label: "seatunnel" },
                        { value: "datax", label: "datax（需先配镜像）" },
                        { value: "flink", label: "flink" },
                      ]}
                    />
                  </Form.Item>
                </>
              ),
            },
            {
              key: "shape",
              label: "DAG 形状与时序",
              children: (
                <>
                  <Space align="start" wrap>
                    <Form.Item
                      label="单 DAG 最大任务数"
                      name="max_tasks_per_dag"
                      extra="超出按此拆成多个 DAG"
                    >
                      <InputNumber min={1} max={1000} style={{ width: 160 }} />
                    </Form.Item>
                    <Form.Item
                      label="单 DAG 并发上限"
                      name="max_active_tasks_per_dag"
                      extra="层内不再一次性全放开"
                    >
                      <InputNumber min={1} max={256} style={{ width: 160 }} />
                    </Form.Item>
                  </Space>
                  <Space align="start" wrap>
                    <Form.Item
                      label="等 DAG 解析超时（秒）"
                      name="dag_parse_timeout"
                      extra="要大于 Airflow 的 dag_dir_list_interval（默认 300s），否则首次提交必报「尚未解析到」"
                    >
                      <InputNumber min={0} max={3600} style={{ width: 200 }} />
                    </Form.Item>
                    <Form.Item
                      label="自检探针超时（秒）"
                      name="preflight_sentinel_timeout"
                      extra="提交前自检写一个 sentinel DAG，等它被解析到"
                    >
                      <InputNumber min={0} max={600} style={{ width: 200 }} />
                    </Form.Item>
                  </Space>
                  <Form.Item
                    label="全量装载走 staging + 原子切换"
                    name="staging_swap"
                    valuePropName="checked"
                    extra="先搬进 staging 表、成功后再切换；关掉则直接写正式表，搬到一半失败会留下残缺数据"
                  >
                    <Switch />
                  </Form.Item>
                </>
              ),
            },
          ]}
        />
        <Form.Item>
          <Space>
            <Button type="primary" onClick={() => void save()} loading={saving}>
              保存
            </Button>
            <Button
              icon={<ThunderboltOutlined />}
              onClick={() => void test()}
              loading={testing}
            >
              测试连接
            </Button>
          </Space>
        </Form.Item>
      </Form>
      <Divider plain>
        搬运连接配置（存在 sync-runner 一侧）
      </Divider>
      <SyncRunnerSecretsPanel />
    </>
  );
}
