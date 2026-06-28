#!/usr/bin/env python3
import argparse
import json
import os
import subprocess
import yaml

def run_command(cmd, env=None):
    res = subprocess.run(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    if res.returncode != 0:
        raise Exception(f"Command failed: {cmd}\nStdout: {res.stdout}\nStderr: {res.stderr}")
    return res.stdout

def update_daemonset(context, cpu_limit, mem_limit, cpu_request, mem_request):
    print("Updating DaemonSet resources...")
    cmd = f"kubectl --context {context} get daemonset fluentbit-gke -n kube-system -o json"
    ds_json_str = run_command(cmd)
    ds = json.loads(ds_json_str)
    
    # Locate fluentbit container
    containers = ds["spec"]["template"]["spec"]["containers"]
    fluentbit_container = None
    for c in containers:
        if c["name"] == "fluentbit":
            fluentbit_container = c
            break
            
    if not fluentbit_container:
        raise Exception("Container 'fluentbit' not found in DaemonSet!")
        
    resources = fluentbit_container.setdefault("resources", {})
    limits = resources.setdefault("limits", {})
    requests = resources.setdefault("requests", {})
    
    if cpu_limit is not None:
        if cpu_limit.lower() == "null":
            limits.pop("cpu", None)
        else:
            limits["cpu"] = cpu_limit
            
    if mem_limit is not None:
        if mem_limit.lower() == "null":
            limits.pop("memory", None)
        else:
            limits["memory"] = mem_limit
            
    if cpu_request is not None:
        if cpu_request.lower() == "null":
            requests.pop("cpu", None)
        else:
            requests["cpu"] = cpu_request
            
    if mem_request is not None:
        if mem_request.lower() == "null":
            requests.pop("memory", None)
        else:
            requests["memory"] = mem_request
            
    # Apply updated DS
    updated_ds_str = json.dumps(ds)
    apply_cmd = f"kubectl --context {context} apply -f - << 'EOF'\n{updated_ds_str}\nEOF"
    run_command(apply_cmd)
    print("DaemonSet resource configuration applied.")

def update_configmap(context, workers, kubelet_on):
    print("Updating ConfigMap parameters...")
    cmd = f"kubectl --context {context} get configmap fluentbit-gke-config -n kube-system -o json"
    cm_json_str = run_command(cmd)
    cm = json.loads(cm_json_str)
    
    # fluent-bit.yaml is inside cm["data"]["fluent-bit.yaml"]
    fb_yaml_str = cm["data"]["fluent-bit.yaml"]
    fb_config = yaml.safe_load(fb_yaml_str)
    
    updated = False
    
    # Update workers in outputs
    if workers is not None:
        outputs = fb_config.get("pipeline", {}).get("outputs", [])
        for out in outputs:
            alias = out.get("Alias", out.get("alias"))
            if out.get("name") == "stackdriver" and alias == "node-logging-k8s-container":
                out["workers"] = workers
                updated = True
                print(f"Set stackdriver node-logging-k8s-container workers = {workers}")
            # Also update others if requested, or just node-logging-k8s-container
            if out.get("name") == "stackdriver" and alias in ["node-logging-k8s-pod", "node-logging-k8s-node"]:
                out["workers"] = workers
                
    # Update Use_Kubelet in filters
    if kubelet_on is not None:
        filters = fb_config.get("pipeline", {}).get("filters", [])
        for filt in filters:
            if filt.get("name") == "kubernetes":
                filt["Use_Kubelet"] = "On" if kubelet_on else "Off"
                updated = True
                print(f"Set kubernetes filter Use_Kubelet = {'On' if kubelet_on else 'Off'}")
                
    if updated:
        # Dump back to yaml string
        cm["data"]["fluent-bit.yaml"] = yaml.dump(fb_config)
        # Apply updated ConfigMap
        updated_cm_str = json.dumps(cm)
        apply_cmd = f"kubectl --context {context} apply -f - << 'EOF'\n{updated_cm_str}\nEOF"
        run_command(apply_cmd)
        print("ConfigMap parameters applied.")
    else:
        print("No ConfigMap updates required.")

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--context", required=True)
    parser.add_argument("--workers", type=int, default=None)
    parser.add_argument("--kubelet", choices=["On", "Off", "on", "off"], default=None)
    parser.add_argument("--cpu-limit", default=None)
    parser.add_argument("--mem-limit", default=None)
    parser.add_argument("--cpu-request", default=None)
    parser.add_argument("--mem-request", default=None)
    args = parser.parse_args()
    
    context = args.context
    
    # 1. Update configmap
    kubelet_on = None
    if args.kubelet is not None:
        kubelet_on = args.kubelet.lower() == "on"
    update_configmap(context, args.workers, kubelet_on)
    
    # 2. Update DaemonSet
    if any(p is not None for p in [args.cpu_limit, args.mem_limit, args.cpu_request, args.mem_request]):
        update_daemonset(context, args.cpu_limit, args.mem_limit, args.cpu_request, args.mem_request)
        
    # 3. Restart DaemonSet and wait
    print("Performing rollout restart of fluentbit-gke DaemonSet...")
    run_command(f"kubectl --context {context} rollout restart daemonset/fluentbit-gke -n kube-system")
    run_command(f"kubectl --context {context} rollout status daemonset/fluentbit-gke -n kube-system --timeout=180s")
    print("DaemonSet successfully rolled out and running.")

if __name__ == "__main__":
    main()
