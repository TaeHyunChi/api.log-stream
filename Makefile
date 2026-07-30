IMAGE    ?= log-stream-service
TAG      ?= 0.1.2
NS       ?= oncloud-ai
REGISTRY ?= oncloud-ai.co.kr:5000/oncloud-ai
NODE     ?= k3s-master
WORKER   ?= k3s-worker
# k3s 노드 아키텍처가 빌드 머신과 다르면 지정한다. 예: make build PLATFORM=linux/amd64
# 이 클러스터의 노드는 arm64라 평소에는 비워 둔다.
PLATFORM ?=

REMOTE_IMAGE = $(REGISTRY)/$(IMAGE):$(TAG)

.PHONY: venv install test lint fmt run build push seed-worker deploy undeploy logs restart kong-unregister

venv:
	python3 -m venv .venv

install: venv
	.venv/bin/pip install -r requirements-dev.txt

test:
	.venv/bin/python -m pytest

lint:
	.venv/bin/ruff check .

fmt:
	.venv/bin/ruff format .

run:  ## 로컬 개발 서버 (DB는 docker-compose의 mariadb 사용)
	FLASK_APP=wsgi.py JSON_LOGS=false .venv/bin/flask run --host 0.0.0.0 --port 8205 --debug

build:
	docker build $(if $(PLATFORM),--platform $(PLATFORM),) -t $(IMAGE):$(TAG) .

## 빌드한 이미지를 multipass로 k3s 노드에 넣고, 거기서 사내 레지스트리로 push 한다.
## 개발 머신에서 레지스트리로 직접 push 하지 않는 이유는 레지스트리가 사내망 HTTP라
## Docker Desktop 쪽에 insecure-registries 설정이 따로 필요하기 때문이다.
push: build
	docker save $(IMAGE):$(TAG) -o /tmp/$(IMAGE)-$(TAG).tar
	multipass transfer /tmp/$(IMAGE)-$(TAG).tar $(NODE):/tmp/$(IMAGE)-$(TAG).tar
	multipass exec $(NODE) -- sudo k3s ctr images import /tmp/$(IMAGE)-$(TAG).tar
	multipass exec $(NODE) -- sudo k3s ctr images tag docker.io/library/$(IMAGE):$(TAG) $(REMOTE_IMAGE)
	multipass exec $(NODE) -- sudo k3s ctr images push --plain-http $(REMOTE_IMAGE)
	multipass exec $(NODE) -- rm -f /tmp/$(IMAGE)-$(TAG).tar
	rm -f /tmp/$(IMAGE)-$(TAG).tar
	@echo "pushed: $(REMOTE_IMAGE)"

## 워커 노드 containerd 에 이미지를 직접 넣는 비상용 타깃.
##
## 평소에는 필요 없다 — 두 노드 모두 /etc/hosts 와 cloud-init 템플릿에
## "192.168.252.2 oncloud-ai.co.kr" 이 있어 워커가 레지스트리에서 직접 pull 한다.
## 레지스트리나 이름 해석이 망가졌을 때만 쓴다(imagePullPolicy: IfNotPresent 라
## 로컬에 이미지가 있으면 그것을 쓴다). 먼저 `make build` 로 tar 를 만들어야 한다.
seed-worker:
	docker save $(IMAGE):$(TAG) -o /tmp/$(IMAGE)-$(TAG).tar
	multipass transfer /tmp/$(IMAGE)-$(TAG).tar $(WORKER):/tmp/$(IMAGE)-$(TAG).tar
	multipass exec $(WORKER) -- sudo k3s ctr images import /tmp/$(IMAGE)-$(TAG).tar
	multipass exec $(WORKER) -- sudo k3s ctr images tag docker.io/library/$(IMAGE):$(TAG) $(REMOTE_IMAGE)
	multipass exec $(WORKER) -- rm -f /tmp/$(IMAGE)-$(TAG).tar
	rm -f /tmp/$(IMAGE)-$(TAG).tar

## 스키마 마이그레이션 → 서비스 기동 → Kong 등록 순서로 올린다.
## Job은 완료 후 spec 수정이 불가능해서 지우고 다시 만든다(둘 다 멱등한 작업).
deploy:
	kubectl apply -f deploy/k3s/20-configmap.yaml -f deploy/k3s/30-rbac.yaml
	kubectl apply -f deploy/k3s/40-deployment.yaml -f deploy/k3s/50-service.yaml
	kubectl -n $(NS) rollout status deployment/log-stream-service
	kubectl -n $(NS) delete job log-stream-service-kong-register --ignore-not-found
	kubectl apply -f deploy/k3s/60-kong-register.yaml
	kubectl -n $(NS) wait --for=condition=complete job/log-stream-service-kong-register --timeout=120s
	kubectl -n $(NS) logs job/log-stream-service-kong-register

restart:
	kubectl -n $(NS) rollout restart deployment/log-stream-service
	kubectl -n $(NS) rollout status deployment/log-stream-service

## Deployment/Service만 내린다. deploy/k3s 를 통째로 delete 하면 네임스페이스가
## 지워지면서 같은 네임스페이스의 MariaDB까지 날아간다.
undeploy:
	kubectl delete -f deploy/k3s/40-deployment.yaml -f deploy/k3s/50-service.yaml --ignore-not-found
	kubectl -n $(NS) delete job log-stream-service-kong-register --ignore-not-found

kong-unregister:
	kubectl -n $(NS) run kong-unregister-$$$$ --rm -i --restart=Never --command \
	  --image=$(REGISTRY)/$(IMAGE):$(TAG) -- python -c "\
import urllib.request as u;\
[u.urlopen(u.Request('http://kong-admin.kong.svc.cluster.local:8001'+p, method='DELETE')) for p in ['/routes/log-stream-service-v1','/services/notification-service']];\
print('kong 등록 해제 완료')"

logs:
	kubectl -n $(NS) logs -l app.kubernetes.io/name=log-stream-service -f --tail=100
