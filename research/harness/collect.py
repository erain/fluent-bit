#!/usr/bin/env python3
import argparse
import json
import os
import re
import socket
import subprocess
import time

def find_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('', 0))
        return s.getsockname()[1]

def run_command(cmd, env=None):
    res = subprocess.run(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    if res.returncode != 0:
        raise Exception(f"Command failed: {cmd}\nStdout: {res.stdout}\nStderr: {res.stderr}")
    return res.stdout

def parse_prometheus(text):
    metrics = {}
    for line in text.splitlines():
        if line.startswith('#') or not line.strip():
            continue
        if '{' in line:
            name, rest = line.split('{', 1)
            labels_str, val_str = rest.rsplit('}', 1)
            try:
                val = float(val_str.strip())
            except ValueError:
                continue
            labels = {}
            # Parse comma-separated labels safely
            # Note: labels can contain quotes
            for pair in re.split(r',(?=(?:[^"]*"[^"]*")*[^"]*$)', labels_str):
                if '=' in pair:
                    k, v = pair.split('=', 1)
                    labels[k.strip()] = v.strip().strip('"')
            metrics.setdefault(name, []).append((labels, val))
        elif ' ' in line:
            name, val_str = line.split(' ', 1)
            try:
                metrics[name] = float(val_str.strip())
            except ValueError:
                continue
    return metrics

def parse_top_pod(text):
    cpu_total = 0.0
    mem_total = 0.0
    for line in text.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 3:
            cpu_str = parts[1]
            mem_str = parts[2]
            
            if cpu_str.endswith('m'):
                cpu = float(cpu_str[:-1]) / 1000.0
            else:
                cpu = float(cpu_str)
                
            if mem_str.endswith('Mi'):
                mem = float(mem_str[:-2])
            elif mem_str.endswith('Gi'):
                mem = float(mem_str[:-2]) * 1024.0
            elif mem_str.endswith('Ki'):
                mem = float(mem_str[:-2]) / 1024.0
            else:
                mem = float(mem_str)
                
            cpu_total += cpu
            mem_total += mem
    return cpu_total, mem_total

def get_pods(context):
    cmd = f"kubectl --context {context} get pods -n kube-system -l k8s-app=fluentbit-gke -o json"
    out = run_command(cmd)
    data = json.loads(out)
    return [item['metadata']['name'] for item in data['items']]

def scrape_pod_metrics(context, pod):
    port = find_free_port()
    # Start port-forward
    pf_cmd = f"kubectl --context {context} port-forward -n kube-system pod/{pod} {port}:2020"
    pf_proc = subprocess.Popen(pf_cmd, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    
    # Wait for port to open
    time.sleep(2.5)
    
    try:
        storage_out = run_command(f"curl -s --connect-timeout 5 http://localhost:{port}/api/v1/storage")
        storage_data = json.loads(storage_out)
    except Exception as e:
        pf_proc.terminate()
        raise Exception(f"Failed to fetch storage metrics from {pod}: {e}")
        
    try:
        prometheus_out = run_command(f"curl -s --connect-timeout 5 http://localhost:{port}/api/v2/metrics/prometheus")
        prometheus_data = parse_prometheus(prometheus_out)
    except Exception as e:
        pf_proc.terminate()
        raise Exception(f"Failed to fetch prometheus metrics from {pod}: {e}")
        
    pf_proc.terminate()
    pf_proc.wait()
    return storage_data, prometheus_data

def collect_all(context):
    pods = get_pods(context)
    storage_results = {}
    prometheus_results = {}
    for pod in pods:
        # Retry up to 3 times on scrape failures
        for attempt in range(3):
            try:
                storage_data, prometheus_data = scrape_pod_metrics(context, pod)
                storage_results[pod] = storage_data
                prometheus_results[pod] = prometheus_data
                break
            except Exception as e:
                if attempt == 2:
                    raise e
                time.sleep(1)
    return storage_results, prometheus_results

def query_bq_log_count(project, cluster, namespace, gen_id, start_epoch, end_epoch, token):
    import urllib.request
    import json
    
    url = f"https://bigquery.googleapis.com/bigquery/v2/projects/{project}/queries"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    
    sql = (
        f"SELECT count(*) as log_count "
        f"FROM `{project}.gke_default_logs._AllLogs` "
        f"WHERE timestamp >= TIMESTAMP_SECONDS({int(start_epoch) - 10}) "
        f"  AND timestamp <= TIMESTAMP_SECONDS({int(end_epoch) + 300}) "
        f"  AND JSON_VALUE(resource.labels.cluster_name) = '{cluster}' "
        f"  AND JSON_VALUE(resource.labels.namespace_name) = '{namespace}' "
        f"  AND JSON_VALUE(json_payload.gen_id) = '{gen_id}'"
    )
    
    body = {
        "query": sql,
        "useLegacySql": False
    }
    
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req) as response:
            resp_data = json.loads(response.read().decode())
            rows = resp_data.get("rows", [])
            if rows and len(rows) > 0:
                val = rows[0].get("f", [])[0].get("v", "0")
                return int(val)
            return 0
    except Exception as e:
        print(f"Warning: Failed to query BigQuery: {e}")
        return -1

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--context", required=True)
    parser.add_argument("--project", default="timbai-gke-dev")
    parser.add_argument("--window", type=int, default=600)
    parser.add_argument("--gen-id", default=None)
    parser.add_argument("--rate", type=int, default=0) # loadgen rate per replica
    parser.add_argument("--replicas", type=int, default=0) # loadgen replica count
    args = parser.parse_args()

    context = args.context
    window = args.window
    gen_id = args.gen_id

    # Inherit current env
    env = os.environ.copy()

    print(f"Scraping T0 metrics for context {context}...")
    t0_time = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    t0_epoch = time.time()
    t0_storage, t0_prom = collect_all(context)

    print(f"Waiting {window} seconds for the measurement window...")
    # Sleep window
    time.sleep(window)

    print("Scraping T1 metrics...")
    t1_time = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    t1_epoch = time.time()
    t1_storage, t1_prom = collect_all(context)
    actual_window = t1_epoch - t0_epoch

    # Get resource usage
    print("Scraping resource usage via top...")
    top_out = run_command(f"kubectl --context {context} top pod -n kube-system -l k8s-app=fluentbit-gke")
    cpu_cores, mem_mb = parse_top_pod(top_out)

    # Compute metrics
    # Sum output records/bytes over output targets
    # Targets are: node-logging-k8s-container, node-logging-k8s-pod, node-logging-k8s-node
    target_names = ["node-logging-k8s-container", "node-logging-k8s-pod", "node-logging-k8s-node"]
    
    t0_records = 0.0
    t0_bytes = 0.0
    t0_errors = 0.0
    t0_retries = 0.0
    t0_retries_failed = 0.0
    t0_dropped = 0.0

    for pod, metrics in t0_prom.items():
        # processed records
        for labels, val in metrics.get("fluentbit_output_proc_records_total", []):
            if labels.get("name") in target_names:
                t0_records += val
        # processed bytes
        for labels, val in metrics.get("fluentbit_output_proc_bytes_total", []):
            if labels.get("name") in target_names:
                t0_bytes += val
        # errors
        for labels, val in metrics.get("fluentbit_output_errors_total", []):
            if labels.get("name") in target_names:
                t0_errors += val
        # retries
        for labels, val in metrics.get("fluentbit_output_retries_total", []):
            if labels.get("name") in target_names:
                t0_retries += val
        # failed retries
        for labels, val in metrics.get("fluentbit_output_retries_failed_total", []):
            if labels.get("name") in target_names:
                t0_retries_failed += val
        # dropped
        for labels, val in metrics.get("fluentbit_output_dropped_records_total", []):
            if labels.get("name") in target_names:
                t0_dropped += val

    t1_records = 0.0
    t1_bytes = 0.0
    t1_errors = 0.0
    t1_retries = 0.0
    t1_retries_failed = 0.0
    t1_dropped = 0.0

    for pod, metrics in t1_prom.items():
        # processed records
        for labels, val in metrics.get("fluentbit_output_proc_records_total", []):
            if labels.get("name") in target_names:
                t1_records += val
        # processed bytes
        for labels, val in metrics.get("fluentbit_output_proc_bytes_total", []):
            if labels.get("name") in target_names:
                t1_bytes += val
        # errors
        for labels, val in metrics.get("fluentbit_output_errors_total", []):
            if labels.get("name") in target_names:
                t1_errors += val
        # retries
        for labels, val in metrics.get("fluentbit_output_retries_total", []):
            if labels.get("name") in target_names:
                t1_retries += val
        # failed retries
        for labels, val in metrics.get("fluentbit_output_retries_failed_total", []):
            if labels.get("name") in target_names:
                t1_retries_failed += val
        # dropped
        for labels, val in metrics.get("fluentbit_output_dropped_records_total", []):
            if labels.get("name") in target_names:
                t1_dropped += val

    delta_records = max(0.0, t1_records - t0_records)
    delta_bytes = max(0.0, t1_bytes - t0_bytes)
    delta_errors = max(0.0, t1_errors - t0_errors)
    delta_retries = max(0.0, t1_retries - t0_retries)
    delta_retries_failed = max(0.0, t1_retries_failed - t0_retries_failed)
    delta_dropped = max(0.0, t1_dropped - t0_dropped)

    egress_records_s = delta_records / actual_window
    egress_bytes_s = delta_bytes / actual_window

    # Backlog trend from storage chunks
    t0_total_chunks = sum(pod_data["storage_layer"]["chunks"]["total_chunks"] for pod_data in t0_storage.values())
    t1_total_chunks = sum(pod_data["storage_layer"]["chunks"]["total_chunks"] for pod_data in t1_storage.values())
    
    if t1_total_chunks > t0_total_chunks:
        backlog_trend = "up"
    elif t1_total_chunks < t0_total_chunks:
        backlog_trend = "down"
    else:
        backlog_trend = "flat"

    # Delivery verification
    delivered_count = 0
    generated_count = 0
    delivery_ratio = 1.0
    if gen_id and args.rate > 0 and args.replicas > 0:
        generated_count = args.rate * args.replicas * int(actual_window)
        # Refresh access token dynamically
        token = ""
        try:
            token_cmd = "gcloud auth application-default print-access-token"
            token = subprocess.run(token_cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True).stdout.strip()
        except Exception as e:
            print(f"Warning: Failed to refresh access token: {e}")
        # Query BigQuery log count
        print("Waiting 180 seconds for BigQuery logs to stabilize...")
        time.sleep(180)
        print(f"Querying BigQuery for log count between {t0_time} and {t1_time}...")
        try:
            cluster_name = context.split("_")[-1]
            delivered_count = query_bq_log_count(args.project, cluster_name, "lab-loadgen", gen_id, t0_epoch, t1_epoch, token)
            if generated_count > 0 and delivered_count >= 0:
                delivery_ratio = min(1.0, delivered_count / generated_count)
            else:
                delivery_ratio = -1.0
        except Exception as e:
            print(f"Warning: BigQuery query failed: {e}")
            delivery_ratio = -1.0

    # Per-core efficiency
    per_core_efficiency = 0
    if cpu_cores > 0:
        per_core_efficiency = int(egress_records_s / cpu_cores)

    result = {
        "actual_window_s": round(actual_window, 2),
        "egress_records_s": int(egress_records_s),
        "egress_bytes_s": int(egress_bytes_s),
        "cpu_cores_used": round(cpu_cores, 3),
        "mem_used_mb": int(mem_mb),
        "per_core_efficiency": per_core_efficiency,
        "retries": int(delta_retries),
        "retries_failed": int(delta_retries_failed),
        "errors": int(delta_errors),
        "dropped": int(delta_dropped),
        "backlog_trend": backlog_trend,
        "t0_total_chunks": t0_total_chunks,
        "t1_total_chunks": t1_total_chunks,
        "generated_records": generated_count,
        "delivered_records": delivered_count,
        "delivery_ratio": round(delivery_ratio, 4) if delivery_ratio >= 0 else -1.0
    }
    
    print(json.dumps(result, indent=2))

if __name__ == "__main__":
    main()
