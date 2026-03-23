FROM us-central1-docker.pkg.dev/bespokelabs/nebula-devops-registry/nebula-devops:1.0.3

ENV ALLOWED_NAMESPACES="default,bleater"

USER root

RUN apt-get update && apt-get install -y curl openssl python3 && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL https://github.com/google/go-containerregistry/releases/download/v0.19.0/go-containerregistry_Linux_x86_64.tar.gz | tar xz -C /usr/local/bin crane

RUN crane pull busybox:latest /busybox.tar
RUN crane pull bitnami/kubectl:latest /kubectl.tar

RUN mkdir -p /grader /mcp_server/tests

COPY setup.sh /setup.sh
COPY solution.sh /solution.sh
COPY grader.py /grader/grader.py
COPY grader.py /mcp_server/tests/grader.py

RUN chmod +x /setup.sh /solution.sh
RUN chmod 700 /grader
