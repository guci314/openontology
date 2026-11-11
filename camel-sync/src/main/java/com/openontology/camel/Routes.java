package com.openontology.camel;

import jakarta.enterprise.context.ApplicationScoped;
import org.apache.camel.AggregationStrategy;
import org.apache.camel.Exchange;
import org.apache.camel.LoggingLevel;
import org.apache.camel.builder.RouteBuilder;
import org.apache.camel.model.rest.RestBindingMode;
import org.eclipse.microprofile.config.ConfigProvider;

import java.util.ArrayList;
import java.util.List;

/**
 * Real Camel EIP routes for the dual-ERP integration bus.
 *
 * <pre>
 *   POST /camel/ingest    -> direct:ingest    -> OdooJsonBean.ingest
 *   POST /camel/writeback -> direct:writeback
 *       split(body()) -> choice()
 *           when body[region] == north -> direct:writeback-north
 *           when body[region] == south -> direct:writeback-south
 *   GET  /camel/stats     -> direct:stats
 * </pre>
 *
 * The timer route is opt-in via {@code camel.sync.auto.enabled=true}.
 */
@ApplicationScoped
public class Routes extends RouteBuilder {

    @Override
    public void configure() {
        onException(Exception.class)
                .handled(true)
                .log(LoggingLevel.ERROR, "camel-sync-error", "${exception.message}")
                .to("bean:odooSync?method=errorResponse");

        restConfiguration()
                .component("platform-http")
                .bindingMode(RestBindingMode.off);

        rest("/camel")
                .post("/ingest").to("direct:ingest")
                .post("/writeback").to("direct:writeback")
                .get("/stats").to("direct:stats");

        from("direct:ingest")
                .routeId("camel-ingest")
                .to("bean:odooSync?method=ingest")
                .setHeader(Exchange.CONTENT_TYPE, constant("application/json"));

        from("direct:writeback")
                .routeId("camel-writeback")
                .to("bean:odooSync?method=pendingWritebacks")
                .split(body(), new CollectListAggregationStrategy())
                    .choice()
                        .when(simple("${body[region]} == 'north'"))
                            .to("direct:writeback-north")
                        .when(simple("${body[region]} == 'south'"))
                            .to("direct:writeback-south")
                        .otherwise()
                            .to("direct:writeback-unknown")
                    .end()
                .end()
                .to("bean:odooSync?method=writebackResponse")
                .setHeader(Exchange.CONTENT_TYPE, constant("application/json"));

        from("direct:writeback-north")
                .routeId("camel-writeback-north")
                .to("bean:odooSync?method=applyWritebackNorth");

        from("direct:writeback-south")
                .routeId("camel-writeback-south")
                .to("bean:odooSync?method=applyWritebackSouth");

        from("direct:writeback-unknown")
                .routeId("camel-writeback-unknown")
                .process(exchange -> {
                    java.util.Map<String, Object> result = new java.util.LinkedHashMap<>();
                    result.put("error", "unknown region");
                    result.put("verified", false);
                    exchange.getMessage().setBody(result);
                });

        from("direct:stats")
                .routeId("camel-stats")
                .to("bean:odooSync?method=stats")
                .setHeader(Exchange.CONTENT_TYPE, constant("application/json"));

        boolean autoSync = ConfigProvider.getConfig()
                .getOptionalValue("camel.sync.auto.enabled", Boolean.class)
                .orElse(false);
        if (autoSync) {
            from("timer:autoSync?period={{camel.sync.auto.period}}&delay={{camel.sync.auto.delay}}")
                    .routeId("camel-auto-sync")
                    .to("direct:ingest");
        }
    }

    /**
     * Collects the result of each split branch into one list so the REST
     * response can contain every writeback evidence row.
     */
    static final class CollectListAggregationStrategy implements AggregationStrategy {
        @Override
        public Exchange aggregate(Exchange oldExchange, Exchange newExchange) {
            Object value = newExchange.getMessage().getBody();

            if (oldExchange == null) {
                List<Object> list = new ArrayList<>();
                list.add(value);
                newExchange.getMessage().setBody(list);
                return newExchange;
            }

            Object body = oldExchange.getMessage().getBody();
            List<Object> list;
            if (body instanceof List<?>) {
                @SuppressWarnings("unchecked")
                List<Object> cast = (List<Object>) body;
                list = cast;
            } else {
                list = new ArrayList<>();
                if (body != null) {
                    list.add(body);
                }
                oldExchange.getMessage().setBody(list);
            }
            list.add(value);
            return oldExchange;
        }
    }
}
