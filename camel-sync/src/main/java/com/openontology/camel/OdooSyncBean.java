package com.openontology.camel;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import io.quarkus.runtime.annotations.RegisterForReflection;
import jakarta.enterprise.context.ApplicationScoped;
import jakarta.inject.Named;
import org.apache.camel.Exchange;
import org.eclipse.microprofile.config.inject.ConfigProperty;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;
import java.util.ArrayList;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.atomic.AtomicInteger;

/**
 * Odoo JSON-RPC extractor/transformer and writeback executor.
 *
 * All Odoo-specific knowledge lives in this PSM bean.  The Python ontology
 * runtime only sees business snapshot JSON and business action runs.
 */
@ApplicationScoped
@Named("odooSync")
@RegisterForReflection
public class OdooSyncBean {

    private static final ObjectMapper M = new ObjectMapper();
    private static final List<String> ORDER_FIELDS = List.of(
            "id", "name", "client_order_ref", "state", "amount_total", "note"
    );

    private final HttpClient http = HttpClient.newBuilder()
            .version(HttpClient.Version.HTTP_1_1)
            .connectTimeout(Duration.ofSeconds(10))
            .build();

    private final Map<String, Long> uids = new ConcurrentHashMap<>();
    private final AtomicInteger ingestCount = new AtomicInteger();
    private final AtomicInteger northWritebacks = new AtomicInteger();
    private final AtomicInteger southWritebacks = new AtomicInteger();
    private final java.util.Set<String> processedRunIds = ConcurrentHashMap.newKeySet();
    private volatile String lastError;

    @ConfigProperty(name = "odoo.url", defaultValue = "http://localhost:8069/jsonrpc")
    String odooUrl;

    @ConfigProperty(name = "odoo.user", defaultValue = "admin")
    String odooUser;

    @ConfigProperty(name = "odoo.password", defaultValue = "admin")
    String odooPassword;

    @ConfigProperty(name = "odoo.db.north", defaultValue = "erp_north")
    String dbNorth;

    @ConfigProperty(name = "odoo.db.south", defaultValue = "erp_south")
    String dbSouth;

    @ConfigProperty(name = "ontology.url", defaultValue = "http://127.0.0.1:8000")
    String ontologyUrl;

    // ------------------------------------------------------------------
    // Ingest route: both Odoos -> business snapshot -> ontology HTTP
    // ------------------------------------------------------------------
    public String ingest() throws Exception {
        ArrayNode customers = M.createArrayNode();
        ArrayNode orders = M.createArrayNode();
        ArrayNode linkPairs = M.createArrayNode();

        List<DbSpec> specs = List.of(
                new DbSpec(dbNorth, "north", "N-C01", "N-SO"),
                new DbSpec(dbSouth, "south", "S-C01", "S-SO")
        );

        for (DbSpec spec : specs) {
            JsonNode partners = searchRead(
                    spec.db(), "res.partner",
                    domainEquals("ref", spec.partnerRef()),
                    List.of("id", "name", "ref")
            );
            if (!partners.isArray() || partners.size() == 0) {
                throw new IllegalStateException(
                        "customer " + spec.partnerRef() + " missing from " + spec.db()
                );
            }
            JsonNode partner = partners.get(0);

            ObjectNode customer = M.createObjectNode();
            customer.put("customer_id", spec.partnerRef());
            customer.put("name", partner.path("name").asText());
            customer.put("region", spec.region());
            customers.add(customer);

            JsonNode rows = searchRead(
                    spec.db(), "sale.order",
                    domainNotEqualsFalse("client_order_ref"),
                    ORDER_FIELDS
            );
            for (JsonNode row : rows) {
                String orderId = row.path("client_order_ref").asText("");
                if (!orderId.startsWith(spec.orderPrefix())) {
                    continue;
                }
                ObjectNode order = M.createObjectNode();
                order.put("order_id", orderId);
                order.put("customer_id", spec.partnerRef());
                order.put("region", spec.region());
                order.put("status", mapStatus(row));
                order.put("total_cents",
                        Math.round(row.path("amount_total").asDouble(0.0) * 100.0));
                orders.add(order);

                ArrayNode pair = M.createArrayNode();
                pair.add(spec.partnerRef());
                pair.add(orderId);
                linkPairs.add(pair);
            }
        }

        ObjectNode payload = M.createObjectNode();
        ObjectNode objects = payload.putObject("objects");
        objects.set("Customer", customers);
        objects.set("Order", orders);
        ObjectNode links = payload.putObject("links");
        links.set("customer_orders", linkPairs);

        String snapshotResponse = httpPost(
                baseUrl(ontologyUrl) + "/api/snapshot",
                M.writeValueAsString(payload)
        );
        ingestCount.incrementAndGet();

        ObjectNode result = M.createObjectNode();
        result.put("ok", true);
        result.put("customers", customers.size());
        result.put("orders", orders.size());
        result.put("links", linkPairs.size());
        result.set("snapshot", M.readTree(snapshotResponse));
        return M.writeValueAsString(result);
    }

    // ------------------------------------------------------------------
    // Writeback route: committed business runs -> split/choice -> Odoo
    // ------------------------------------------------------------------
    public List<Map<String, Object>> pendingWritebacks() throws Exception {
        String raw = httpGet(
                baseUrl(ontologyUrl) + "/api/action-runs?status=COMMITTED"
        );
        JsonNode runs = M.readTree(raw).path("runs");
        List<Map<String, Object>> tasks = new ArrayList<>();

        for (JsonNode run : runs) {
            String runId = run.path("run_id").asText("");
            if (runId.isEmpty() || processedRunIds.contains(runId)) {
                continue;
            }
            if (!"cancel_order".equals(run.path("action_type").asText())) {
                continue;
            }

            for (JsonNode change : run.path("object_diff").path("object_changes")) {
                if (!"Order".equals(change.path("object_type").asText())) {
                    continue;
                }
                if (!"update".equals(change.path("kind").asText())) {
                    continue;
                }
                String orderId = change.path("object_id").asText("");
                String targetStatus = change.path("after").path("status").asText("");
                if (!"cancelled".equals(targetStatus) && !"pending".equals(targetStatus)) {
                    continue;
                }
                String region = orderId.startsWith("N-")
                        ? "north"
                        : orderId.startsWith("S-") ? "south" : "";
                if (region.isEmpty()) {
                    continue;
                }

                Map<String, Object> task = new LinkedHashMap<>();
                task.put("run_id", runId);
                task.put("order_id", orderId);
                task.put("region", region);
                task.put("target_status", targetStatus);
                tasks.add(task);
            }
        }
        return tasks;
    }

    public Map<String, Object> applyWritebackNorth(Map<String, Object> task) throws Exception {
        return applyWriteback("north", dbNorth, task);
    }

    public Map<String, Object> applyWritebackSouth(Map<String, Object> task) throws Exception {
        return applyWriteback("south", dbSouth, task);
    }

    private Map<String, Object> applyWriteback(
            String region, String db, Map<String, Object> task
    ) throws Exception {
        String runId = String.valueOf(task.get("run_id"));
        String orderId = String.valueOf(task.get("order_id"));
        String targetStatus = String.valueOf(task.get("target_status"));

        Map<String, Object> result = new LinkedHashMap<>();
        result.put("run_id", runId);
        result.put("order_id", orderId);
        result.put("region", region);

        if (processedRunIds.contains(runId)) {
            result.put("skipped", true);
            result.put("verified", true);
            return result;
        }

        JsonNode order = findOrder(db, orderId);
        long odooId = order.path("id").asLong();
        String before = order.path("state").asText();
        result.put("odoo_state_before", before);

        if ("cancelled".equals(targetStatus)) {
            if (!"cancel".equals(before)) {
                callOrderAction(db, odooId, "action_cancel");
            }
            String after = orderState(db, orderId);
            if (!"cancel".equals(after)) {
                throw new IllegalStateException(
                        "writeback verification failed for " + orderId
                                + ": expected cancel, got " + after
                );
            }
            result.put("odoo_state_after", after);
            if ("north".equals(region)) {
                northWritebacks.incrementAndGet();
            } else {
                southWritebacks.incrementAndGet();
            }
        } else if ("pending".equals(targetStatus)) {
            if ("cancel".equals(before)) {
                callOrderAction(db, odooId, "action_draft");
            }
            String state = orderState(db, orderId);
            if (!"sale".equals(state)) {
                callOrderAction(db, odooId, "action_confirm");
            }
            String after = orderState(db, orderId);
            if (!"sale".equals(after)) {
                throw new IllegalStateException(
                        "restore verification failed for " + orderId
                                + ": expected sale, got " + after
                );
            }
            result.put("odoo_state_after", after);
            if ("north".equals(region)) {
                northWritebacks.incrementAndGet();
            } else {
                southWritebacks.incrementAndGet();
            }
        } else {
            throw new IllegalArgumentException("unsupported target status: " + targetStatus);
        }

        processedRunIds.add(runId);
        result.put("verified", true);
        return result;
    }

    public String writebackResponse(Object body) throws Exception {
        List<Object> applied = new ArrayList<>();
        if (body instanceof List<?> list) {
            applied.addAll(list);
        } else if (body != null) {
            applied.add(body);
        }

        ObjectNode result = M.createObjectNode();
        result.put("ok", true);
        result.put("count", applied.size());
        result.set("applied", M.valueToTree(applied));
        return M.writeValueAsString(result);
    }

    public String stats() throws Exception {
        ObjectNode result = M.createObjectNode();
        result.put("ok", true);
        result.put("ingest_count", ingestCount.get());
        result.put("north_writebacks", northWritebacks.get());
        result.put("south_writebacks", southWritebacks.get());

        List<String> ids = new ArrayList<>(processedRunIds);
        Collections.sort(ids);
        ArrayNode processed = result.putArray("processed_run_ids");
        for (String id : ids) {
            processed.add(id);
        }
        if (lastError == null) {
            result.putNull("last_error");
        } else {
            result.put("last_error", lastError);
        }
        return M.writeValueAsString(result);
    }

    public String errorResponse(Exchange exchange) throws Exception {
        Throwable caught = exchange.getProperty(Exchange.EXCEPTION_CAUGHT, Throwable.class);
        String message = caught == null
                ? "unknown error"
                : (caught.getMessage() == null
                        ? caught.getClass().getSimpleName()
                        : caught.getMessage());
        lastError = message;

        exchange.getMessage().setHeader(Exchange.HTTP_RESPONSE_CODE, 500);
        exchange.getMessage().setHeader(Exchange.CONTENT_TYPE, "application/json");

        ObjectNode result = M.createObjectNode();
        result.put("ok", false);
        result.put("error", message);
        return M.writeValueAsString(result);
    }

    // ------------------------------------------------------------------
    // Odoo JSON-RPC helpers
    // ------------------------------------------------------------------
    private long login(String db) throws Exception {
        Long cached = uids.get(db);
        if (cached != null) {
            return cached;
        }
        ArrayNode args = M.createArrayNode();
        args.add(db);
        args.add(odooUser);
        args.add(odooPassword);
        JsonNode result = odooRpc("common", "login", args);
        long uid = result.asLong(0);
        if (uid <= 0) {
            throw new IllegalStateException("Odoo login failed for database " + db);
        }
        uids.put(db, uid);
        return uid;
    }

    private JsonNode odooRpc(String service, String method, ArrayNode args) throws Exception {
        ObjectNode payload = M.createObjectNode();
        payload.put("jsonrpc", "2.0");
        payload.put("method", "call");
        ObjectNode params = payload.putObject("params");
        params.put("service", service);
        params.put("method", method);
        params.set("args", args);
        payload.put("id", 1);

        String raw = httpPost(odooUrl, M.writeValueAsString(payload));
        JsonNode response = M.readTree(raw);
        if (response.has("error")) {
            JsonNode error = response.path("error");
            String message = error.path("data").path("message")
                    .asText(error.path("message").asText("Odoo JSON-RPC error"));
            throw new IllegalStateException(message);
        }
        return response.path("result");
    }

    private JsonNode executeKw(
            String db, String model, String method,
            ArrayNode callArgs, ObjectNode kwargs
    ) throws Exception {
        long uid = login(db);
        ArrayNode args = M.createArrayNode();
        args.add(db);
        args.add(uid);
        args.add(odooPassword);
        args.add(model);
        args.add(method);
        args.add(callArgs);
        args.add(kwargs);
        return odooRpc("object", "execute_kw", args);
    }

    private JsonNode searchRead(
            String db, String model, ArrayNode domain, List<String> fields
    ) throws Exception {
        ArrayNode callArgs = M.createArrayNode();
        callArgs.add(domain);
        ObjectNode kwargs = M.createObjectNode();
        ArrayNode fieldArray = kwargs.putArray("fields");
        for (String field : fields) {
            fieldArray.add(field);
        }
        return executeKw(db, model, "search_read", callArgs, kwargs);
    }

    private JsonNode findOrder(String db, String orderRef) throws Exception {
        JsonNode rows = searchRead(
                db, "sale.order",
                domainEquals("client_order_ref", orderRef),
                ORDER_FIELDS
        );
        if (!rows.isArray() || rows.size() != 1) {
            throw new IllegalStateException(
                    "expected exactly one sale.order with client_order_ref="
                            + orderRef + " in " + db + ", found "
                            + (rows.isArray() ? rows.size() : -1)
            );
        }
        return rows.get(0);
    }

    private String orderState(String db, String orderRef) throws Exception {
        return findOrder(db, orderRef).path("state").asText();
    }

    private void callOrderAction(String db, long odooId, String method) throws Exception {
        ArrayNode ids = M.createArrayNode();
        ids.add(odooId);
        ArrayNode callArgs = M.createArrayNode();
        callArgs.add(ids);
        executeKw(db, "sale.order", method, callArgs, M.createObjectNode());
    }

    private static ArrayNode domainEquals(String field, String value) {
        ArrayNode domain = M.createArrayNode();
        ArrayNode condition = domain.addArray();
        condition.add(field);
        condition.add("=");
        condition.add(value);
        return domain;
    }

    private static ArrayNode domainNotEqualsFalse(String field) {
        ArrayNode domain = M.createArrayNode();
        ArrayNode condition = domain.addArray();
        condition.add(field);
        condition.add("!=");
        condition.add(false);
        return domain;
    }

    private static String mapStatus(JsonNode row) {
        if ("cancel".equals(row.path("state").asText())) {
            return "cancelled";
        }
        String note = row.path("note").asText("");
        if (note.contains("STATUS:SHIPPED")) {
            return "shipped";
        }
        return "pending";
    }

    private String httpPost(String url, String json) throws Exception {
        HttpRequest request = HttpRequest.newBuilder(URI.create(url))
                .version(HttpClient.Version.HTTP_1_1)
                .header("Content-Type", "application/json")
                .timeout(Duration.ofSeconds(60))
                .POST(HttpRequest.BodyPublishers.ofString(json))
                .build();
        HttpResponse<String> response = http.send(
                request, HttpResponse.BodyHandlers.ofString()
        );
        if (response.statusCode() < 200 || response.statusCode() >= 300) {
            throw new IllegalStateException(
                    "HTTP " + response.statusCode() + " from " + url
                            + ": " + response.body()
            );
        }
        return response.body();
    }

    private String httpGet(String url) throws Exception {
        HttpRequest request = HttpRequest.newBuilder(URI.create(url))
                .version(HttpClient.Version.HTTP_1_1)
                .timeout(Duration.ofSeconds(60))
                .GET()
                .build();
        HttpResponse<String> response = http.send(
                request, HttpResponse.BodyHandlers.ofString()
        );
        if (response.statusCode() < 200 || response.statusCode() >= 300) {
            throw new IllegalStateException(
                    "HTTP " + response.statusCode() + " from " + url
                            + ": " + response.body()
            );
        }
        return response.body();
    }

    private static String baseUrl(String url) {
        return url.endsWith("/") ? url.substring(0, url.length() - 1) : url;
    }

    private record DbSpec(
            String db,
            String region,
            String partnerRef,
            String orderPrefix
    ) {
    }
}
