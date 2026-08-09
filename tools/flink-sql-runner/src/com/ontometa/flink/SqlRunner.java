package com.ontometa.flink;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Paths;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

import org.apache.flink.table.api.EnvironmentSettings;
import org.apache.flink.table.api.TableEnvironment;
import org.apache.flink.table.api.TableResult;

/**
 * ontoMeta 的通用 Flink SqlRunner。
 *
 * <p>契约（由 {@code app/services/airflow_dag_builder.py} 拼命令行）：
 * {@code flink run -c com.ontometa.flink.SqlRunner sql-runner.jar --file <脚本.sql>}。
 *
 * <p>做三件事，一件都不多：
 * <ol>
 *   <li><b>读脚本</b>：ontoMeta 生成的 Flink SQL（SET + CREATE TABLE ×2 + INSERT）。</li>
 *   <li><b>替换凭据占位符</b>：脚本里只有 <code>${ERP_READONLY_URL}</code> 这类占位符，
 *       真值从**环境变量**取——凭据不进产物（不变量 5），所以运行期才补上。
 *       缺哪个变量就报哪个名字，绝不带着空串往下跑：那会得到一句
 *       "Connection refused" 之类完全指不到原因的错。</li>
 *   <li><b>逐条执行</b>：最后一条 INSERT 在 batch 模式下要 {@code await()}，
 *       否则作业提交完就返回，Airflow 会把「已提交」当成「已完成」。</li>
 * </ol>
 */
public final class SqlRunner {

    private static final Pattern PLACEHOLDER = Pattern.compile("\\$\\{([A-Za-z0-9_]+)\\}");

    /** {@code SET 'k' = 'v'} / {@code SET k = v}（sql-client 语法，1.13 的 executeSql 不收）。 */
    private static final Pattern SET_STATEMENT = Pattern.compile(
            "(?is)^\\s*SET\\s+'?([A-Za-z0-9_.\\-]+)'?\\s*=\\s*'?([^']*?)'?\\s*$");

    public static void main(String[] args) throws Exception {
        String file = null;
        for (int i = 0; i + 1 < args.length; i++) {
            if ("--file".equals(args[i])) {
                file = args[i + 1];
            }
        }
        if (file == null) {
            throw new IllegalArgumentException("用法：--file <SQL 脚本路径>");
        }

        String script = new String(Files.readAllBytes(Paths.get(file)), StandardCharsets.UTF_8);
        String resolved = substitute(script);

        List<String> all = split(resolved);
        // SET 由 runner 自己解释：Flink 1.13 的 executeSql() 不收 SET（那是 sql-client
        // 的语法），而 execution.runtime-mode 又必须在建 TableEnvironment 时就定下来
        // ——批/流是两套 planner 配置，建完再改不生效。
        Map<String, String> options = new LinkedHashMap<>();
        List<String> statements = new ArrayList<>();
        for (String stmt : all) {
            Matcher set = SET_STATEMENT.matcher(stmt);
            if (set.matches()) {
                options.put(set.group(1).trim(), set.group(2).trim());
            } else {
                statements.add(stmt);
            }
        }

        String runtimeMode = options.remove("execution.runtime-mode");
        EnvironmentSettings.Builder builder = EnvironmentSettings.newInstance();
        builder = "streaming".equalsIgnoreCase(runtimeMode)
                ? builder.inStreamingMode()
                : builder.inBatchMode();
        TableEnvironment tableEnv = TableEnvironment.create(builder.build());
        for (Map.Entry<String, String> option : options.entrySet()) {
            tableEnv.getConfig().getConfiguration()
                    .setString(option.getKey(), option.getValue());
        }

        System.out.println("[ontometa] 运行模式 " + (runtimeMode == null ? "batch(缺省)" : runtimeMode)
                + "，" + statements.size() + " 条语句，来自 " + file);
        for (int i = 0; i < statements.size(); i++) {
            String stmt = statements.get(i);
            System.out.println("[ontometa] 执行第 " + (i + 1) + " 条：" + preview(stmt));
            TableResult result = tableEnv.executeSql(stmt);
            // INSERT 是异步提交的；不等它结束，Airflow 会把「已提交」当成「已完成」。
            if (stmt.regionMatches(true, 0, "INSERT", 0, 6)) {
                result.await();
            }
        }
        System.out.println("[ontometa] 全部语句执行完毕");
    }

    /** {@code ${NAME}} → 环境变量 NAME。缺失即报错并点名，不静默留空。 */
    static String substitute(String script) {
        Matcher matcher = PLACEHOLDER.matcher(script);
        StringBuffer out = new StringBuffer();
        List<String> missing = new ArrayList<>();
        while (matcher.find()) {
            String name = matcher.group(1);
            String value = System.getenv(name);
            if (value == null) {
                missing.add(name);
                value = "";
            }
            matcher.appendReplacement(out, Matcher.quoteReplacement(value));
        }
        matcher.appendTail(out);
        if (!missing.isEmpty()) {
            throw new IllegalStateException(
                    "缺少凭据环境变量：" + String.join("、", missing)
                            + "（脚本里的占位符由 ontoMeta 按数据源别名生成，"
                            + "需在 Flink 提交环境里按同名环境变量提供）");
        }
        return out.toString();
    }

    /**
     * 按分号切语句，跳过 {@code --} 行注释；分号在单引号里时不算分隔符
     * （连接串、密码里都可能带分号）。
     */
    static List<String> split(String script) {
        List<String> statements = new ArrayList<>();
        StringBuilder current = new StringBuilder();
        boolean inQuote = false;
        boolean inComment = false;
        for (int i = 0; i < script.length(); i++) {
            char c = script.charAt(i);
            if (inComment) {
                if (c == '\n') {
                    inComment = false;
                    current.append(c);
                }
                continue;
            }
            if (!inQuote && c == '-' && i + 1 < script.length() && script.charAt(i + 1) == '-') {
                inComment = true;
                i++;
                continue;
            }
            if (c == '\'') {
                inQuote = !inQuote;
            }
            if (c == ';' && !inQuote) {
                addIfNotBlank(statements, current);
                current.setLength(0);
                continue;
            }
            current.append(c);
        }
        addIfNotBlank(statements, current);
        return statements;
    }

    private static void addIfNotBlank(List<String> statements, StringBuilder buffer) {
        String stmt = buffer.toString().trim();
        if (!stmt.isEmpty()) {
            statements.add(stmt);
        }
    }

    private static String preview(String stmt) {
        String oneLine = stmt.replaceAll("\\s+", " ");
        return oneLine.length() <= 90 ? oneLine : oneLine.substring(0, 90) + "…";
    }

    private SqlRunner() {
    }
}
