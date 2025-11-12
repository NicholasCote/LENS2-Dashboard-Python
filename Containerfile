# Use an official Python runtime as a base image
FROM docker.io/mambaorg/micromamba:latest

USER root

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update

# Set the working directory in the container to /app
WORKDIR /home/mambauser/app

RUN chown mambauser:mambauser /home/mambauser/app

USER mambauser

# Copy environment.yml first
COPY --chown=mambauser src/cesm-2-dashboard/environment.yml .

# Install packages
RUN micromamba env create -f environment.yml

# Copy application code
COPY --chown=mambauser src/cesm-2-dashboard/ .

# Activate the environment by providing ENV_NAME as an environment variable at runtime 
# Make port bokeh application port to the world outside this container
EXPOSE 5006

CMD ["panel", "serve", "app.py", "--allow-websocket-origin=*"]